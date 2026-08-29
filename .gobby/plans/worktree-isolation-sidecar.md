# Keep isolation markers out of tracked project.json

**Plan ID:** worktree-isolation-sidecar

Canonical artifact after approval: `.gobby/plans/worktree-isolation-sidecar.md`. Lightweight Gobby process: no enhancement or adversarial review unless opted in.

## Overview
`kind: framing`

Stop rewriting tracked `.gobby/project.json` when Gobby creates or repairs a worktree or clone. Write a gitignored `.gobby/isolation.json` sidecar with `parent_project_path` and `parent_project_id` instead, so `create_worktree` leaves a clean tree, overlay indexing still resolves the parent, and default-flag `delete_worktree` can succeed. Canonical copy after approval: `.gobby/plans/worktree-isolation-sidecar.md`. Closes #21193.

## Constraints
`kind: framing`

Decision Record (confirmed 2026-08-29):

- Lightweight plan.
- Gitignored sidecar `.gobby/isolation.json` for worktrees **and** clones.
- Never mutate tracked `.gobby/project.json` for isolation. The 100755→100644 mode flip is accidental (`mkstemp` + `os.replace` without `fchmod`) and goes away because the rewrite goes away.
- No legacy reader: parent keys inside `project.json` are not isolation markers. Existing isolated checkouts get a sidecar from the known parent (`main_repo_path` + source `id`) on the next `ensure` / `repair`, then generated `project.json` dirt is restored from `HEAD`.
- Rejected: delete-time special-case (task option b); committing parent keys on the parent checkout (task option c; docs already strip them as nonportable); registry-only (gcode/hooks resolve from the filesystem); git-native worktree-only split (clones share the writer).

Hard constraints:

- Land the Python writer and the gcore/gcode reader in **one PR**. gcode with no-legacy read and no sidecar yet would drop overlay on every current worktree.
- A crate change is live only after rebuild and install via a new inode (`cp` to a dotfile, `mv -f` over `~/.gobby/bin/gcode`). Load the `rust` skill before editing `crates/`.
- Linked worktrees share `.git/info/exclude` with the main checkout, so the sidecar must be ignored by a **committed** `.gitignore` entry, not `info/exclude`.
- `skip-worktree` on `.gobby/project.json` is not the fix; remove that branch. Keep hygiene for `.mcp.json` / `.claude/` / `.codex/` / `.factory/hooks/hooks.json`.
- Do not commit `parent_project_path` / `parent_project_id` on the parent checkout (`docs/guides/configuration.md`, `NONPORTABLE_PROJECT_KEYS`).
- No daemon restart required for Python; gcode install does not require a daemon restart.

Sidecar path and shape (runtime artifact in isolated checkouts, not a file to commit in this repo):

- Path: `<isolated-root>/.gobby/isolation.json`
- JSON object with exactly `parent_project_path` (absolute resolved source repo) and `parent_project_id` (source `id`), indent 2, trailing newline
- Empty strings are missing; both keys must be present together or the marker is invalid
- Relative parent path resolves against the isolated root
- Self-referential path is not an overlay; Gobby never writes that sidecar
- Tracked project metadata remains `<isolated-root>/.gobby/project.json` and must stay byte-for-byte as git checked it out (plus source-bytes copy only when that file is missing)

Production size (all targeted production files are under 850 lines):

- `src/gobby/utils/project_context.py` ends ~422
- `src/gobby/agents/isolation_git_hygiene.py` ends ~195
- `src/gobby/agents/isolation_repair.py` ends ~330
- `src/gobby/mcp_proxy/tools/worktrees/_create.py` ends ~220
- `src/gobby/code_index/eligibility.py` ends ~110
- `crates/gcore/src/project.rs` ends ~318
- `crates/gcode/src/config/context.rs` ends ~794

Consumer sweep (literal; run from this checkout):

```text
gcode grep -w ensure_project_json_for_isolation src tests crates
gcode grep -w copy_project_json_to_worktree src tests
gcode grep -w read_isolation_marker crates src tests
gcode grep -w overlay_project_id_for_root src tests
gcode grep -w is_generated_isolation_project_json src tests
gcode grep -w parent_project_path src/gobby tests crates
```

Owned production consumers of the writer: `isolation_repair.py`, `worktrees/_helpers.py`, `worktrees/_create.py`, `hooks/event_handlers/_misc.py` (`MiscEventHandlerMixin.handle_worktree_create` calls `copy_project_json_to_worktree`; no body change if the wrapper still writes the sidecar).

Owned production consumers of the marker: `crates/gcode/src/config/context.rs` (`resolve_project_identity`), `crates/gcore/src/project.rs` (`code_overlay_project_id`), `eligibility.py`, `get_project_context` / `_build_and_set_project_context` (spawn `_context_from_project_path` already calls `get_project_context`), `plans/symbol_targets.py` (`_resolve_validation_scope` reads context dict keys, not the file), `isolation_git_hygiene.py`, `worktree_reuse.py` (`_blocking_status_lines`), `project_init.py` (`update_project_json_fields` still strips leftover keys from committed `project.json`).

Transparent callers that need no edit if helpers change in place: `src/gobby/hooks/event_handlers/_tool.py` (`overlay_project_id_for_root`), `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py` (`_context_from_project_path`), `src/gobby/hooks/event_handlers/_misc.py` (`handle_worktree_create`), `src/gobby/mcp_proxy/tools/worktrees/__init__.py` (re-export), `src/gobby/agents/isolation_clone.py` / `isolation_worktree.py` / `isolation.py` (call `repair_isolation_environment`), `src/gobby/utils/project_init.py` (`is_generated_isolation_project_json` for leftover-key stripping), `tests/agents/test_isolation_base_capture.py` (patches the kept `ensure_project_json_for_isolation` name), and the many `get_project_context` / `resolve_project_identity` call sites. They stay out of Targets. `gobby plans validate` reports those as consumer-coverage warnings; they are expected.

`workspace_merge.py` already ignores gobby-local dirt and strips `WORKTREE_LOCAL_PROJECT_KEYS` from `project.json` during merge. No change: a gitignored sidecar does not appear in porcelain.

## P1: Sidecar writer and Python identity
`kind: framing`

**Goal**: Isolated checkouts get the sidecar from Constraints and a byte-for-byte inherited project metadata file; Python overlay and workflow parent discovery read the sidecar only.

### 1.1 Write the isolation sidecar and leave tracked project metadata alone [category: code]
`kind: deliverable`

Targets:
- `src/gobby/utils/project_context.py::ensure_project_json_for_isolation`
- `src/gobby/utils/project_context.py::get_project_context`
- `src/gobby/utils/project_context.py::_build_and_set_project_context`
- `src/gobby/mcp_proxy/tools/worktrees/_helpers.py::copy_project_json_to_worktree`
- `src/gobby/mcp_proxy/tools/worktrees/_create.py::create_worktree`
- `src/gobby/agents/isolation_repair.py::repair_isolation_environment`
- `src/gobby/agents/isolation_git_hygiene.py::apply_isolation_git_hygiene`
- `src/gobby/agents/isolation_git_hygiene.py::is_generated_isolation_project_json`
- `src/gobby/agents/worktree_reuse.py::_blocking_status_lines`
- `src/gobby/agents/worktree_reuse.py::_is_generated_isolation_status_path`
- `src/gobby/code_index/eligibility.py::overlay_project_id_for_root`
- `.gitignore`
- `tests/utils/test_project_context.py::TestEnsureProjectJsonForIsolation.test_creates_when_missing`
- `tests/utils/test_project_context.py::TestEnsureProjectJsonForIsolation.test_augments_existing`
- `tests/utils/test_project_context.py::TestEnsureProjectJsonForIsolation.test_replaces_target_atomically`
- `tests/mcp_proxy/tools/test_worktrees_create.py::test_create_worktree_actual_git_path_preserves_project_json_bytes_and_clean_status`
- `tests/mcp_proxy/tools/test_worktrees_create.py::test_create_worktree_preserves_project_json_trailing_newline`
- `tests/mcp_proxy/tools/test_worktrees_helpers.py::TestCopyProjectJsonToWorktree.test_copies_project_json`
- `tests/mcp_proxy/tools/test_worktrees_helpers.py::TestCopyProjectJsonToWorktree.test_augments_existing_with_parent_path`
- `tests/worktrees/test_parent_project_path.py::*` — scope-reason: every test in this file currently asserts parent keys in project.json
- `tests/agents/test_isolation_project_json.py::test_clone_isolation_writes_parent_project_id`
- `tests/agents/test_isolation_project_json.py::test_repair_marks_tracked_project_json_skip_worktree`
- `tests/code_index/test_eligibility.py::_write_isolation_marker`
- `tests/agents/test_worktree_reuse.py::*` — scope-reason: asserts parent keys inside worktree project.json
- `tests/utils/test_utils_project_init.py::*` — scope-reason: one ensure assertion writes parent keys into project.json; portable-key stripping tests in the same file must keep stripping those keys from committed project.json
- `tests/storage/test_project_repo_path_isolation.py::*` — scope-reason: calls ensure and inspects isolated project.json

Keep the function names `ensure_project_json_for_isolation` and `copy_project_json_to_worktree` (a rename is churn across patches). Change their behavior. Sidecar path and JSON shape are in Constraints.

Writer algorithm for `ensure_project_json_for_isolation(source_repo_path, isolated_path)`:

1. If source tracked project metadata is missing, return (same as today).
2. Parse source; require `id`. On `OSError` / `JSONDecodeError` / `KeyError`, raise `IsolationProjectJsonError` (keep the type).
3. Do not `json.dumps` rewrite of existing tracked project metadata. If that file is missing, copy source bytes and source mode (`os.chmod` after replace) so a clone without it still has portable identity. If it exists, leave bytes and mode untouched except step 5.
4. Atomically write the sidecar (`mkstemp` under the isolated `.gobby/` directory, `fsync`, `os.replace`). Sidecar mode 0644 is fine (untracked, gitignored).
5. Cleanup of old dirt, not identity: if `is_generated_isolation_project_json(target, main_repo_path=source_repo_path)` is true, check the tracked metadata back out from `HEAD` and clear skip-worktree on that path. Current dirty worktrees become deletable without treating leftover parent keys as isolation identity going forward.
6. `copy_project_json_to_worktree` stays a one-line delegate.

`create_worktree`: sidecar write is required. Today a failure in `copy_project_json_to_worktree` is logged and the tool still returns success — that leaves a worktree without overlay. On `IsolationProjectJsonError` / `OSError`, roll back the same way as the invalid-`task_id` path: `delete_worktree(..., force=True, delete_branch=create_branch, ...)`, delete the storage row if it was inserted, return `success: False`. `install_provider_hooks` can stay best-effort after a successful sidecar write.

`get_project_context` and `_build_and_set_project_context`: after loading tracked project metadata, **drop** `parent_project_path` / `parent_project_id` from the file dict, then merge those keys from the sidecar if `read_isolation_marker(root)` returns both. Introduce `read_isolation_marker(root: Path) -> dict[str, str] | None` in this module (Python mirror of gcore: sidecar only; `None` on missing/incomplete/malformed, no throw on get-context). `get_workflow_project_path` then keeps working. Spawn already calls `get_project_context`.

`overlay_project_id_for_root`: stop reading parent keys from tracked project metadata. Call `read_isolation_marker`. Self-referential sidecar → `None`. Incomplete → `None` (do not throw; gcode is the loud path).

`apply_isolation_git_hygiene`: drop the skip-worktree / exclude branch for tracked project metadata. Keep `GENERATED_ISOLATION_EXCLUDE_PATHS`. `is_generated_isolation_project_json` remains only for step-5 restore and for `update_project_json_fields` / reuse status filtering of **legacy dirt**.

`worktree_reuse._blocking_status_lines`: after repair has restored tracked metadata, generated dirt on that path should not appear. Porcelain without `--ignored` will not show a gitignored sidecar.

Root `.gitignore`: ignore the sidecar using the path from Constraints. Do not ignore tracked project metadata.

TDD (same leaf): the real-git create fixture's **source** tracked metadata has **no** parent keys and is mode 0755. After `create_worktree(..., use_local=True)`:

- worktree tracked metadata bytes **and** mode match `HEAD` / source
- the sidecar exists with the two parent fields
- `git status --porcelain` in the worktree is empty
- `get_project_context(worktree)` still exposes `parent_project_path`

Also cover: create → commit one extra file → `delete_worktree` with `force=False` succeeds (the delete precheck). Do not duplicate existing merge tests; merge+delete with default flags is that precheck plus merge.

Rewrite `test_repair_marks_tracked_project_json_skip_worktree` into restore-from-HEAD + sidecar write (no skip-worktree on tracked project metadata). Rewrite clone isolation to assert the sidecar, not parent keys in tracked metadata. Point `_write_isolation_marker` at the sidecar so eligibility tests move in one place.

**Acceptance:**

- 1.1.1 - `ensure_project_json_for_isolation` writes the sidecar from Constraints and does not rewrite existing tracked project metadata. symbol: `ensure_project_json_for_isolation`.
- 1.1.2 - Real-git `create_worktree` leaves tracked metadata bytes and mode unchanged and a clean porcelain status, with the sidecar present. test: `tests/mcp_proxy/tools/test_worktrees_create.py::test_create_worktree_actual_git_path_preserves_project_json_bytes_and_clean_status`.
- 1.1.3 - Python context and overlay identity read the sidecar only; leftover parent keys in tracked metadata are ignored. symbol: `get_project_context`.
- 1.1.4 - Repair restores generated tracked-metadata dirt from HEAD and does not skip-worktree that file. test: `tests/agents/test_isolation_project_json.py::test_repair_marks_tracked_project_json_skip_worktree`.
- 1.1.5 - Sidecar write failure rolls back `create_worktree` instead of returning success. symbol: `create_worktree`.
- 1.1.6 - The sidecar path from Constraints is gitignored. file: `.gitignore`.

## P2: gcode overlay reads the sidecar
`kind: framing`

**Goal**: `read_isolation_marker` looks at the sidecar from Constraints only, so overlay indexing matches the Python writer with no tracked-metadata fallback.

### 2.1 Point gcore isolation marker reads at the sidecar [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/project.rs::read_isolation_marker`
- `crates/gcore/src/project.rs::overlay_requires_a_complete_foreign_isolation_marker`
- `crates/gcode/src/config/context.rs::resolve_project_identity`
- `crates/gcode/src/config/tests.rs::self_referential_parent_marker_keeps_project_json_id`
- `crates/gcode/src/config/tests.rs::isolated_marker_with_parent_metadata_resolves_overlay_scope`
- `crates/gcode/src/config/tests.rs::isolated_marker_without_complete_parent_metadata_is_rejected`
- `crates/gcode/src/config/tests.rs::identity_for_cwd_preserves_isolation_errors`
- `crates/gcode/src/project.rs::test_read_isolation_marker_detects_parent_fields`
- `crates/gcore/src/grant/tests.rs::mark_as_worktree`
- `crates/CHANGELOG.md`

Load the `rust` skill. Keep `IsolationMarker` shape.

`read_isolation_marker` must read the sidecar from Constraints only. Do not inspect tracked project metadata. Missing sidecar → `None`. Parent keys left in tracked metadata → `None` (falls through to `LinkedWorktree` / `ProjectJson`). Incomplete sidecar (XOR of the two keys) still errors in `resolve_project_identity`; the XOR error names the sidecar path from Constraints, not the tracked metadata file.

Self-referential sidecar still uses `is_self_referential_isolation_marker` and keeps `ProjectJson` id (put the keys in `self_referential_parent_marker_keeps_project_json_id` on a sidecar whose path resolves to that root). Overlay fixture `isolated_marker_with_parent_metadata_resolves_overlay_scope` writes a complete sidecar beside portable tracked metadata that has **no** parent keys.

Put a small `write_isolation_json` helper next to `write_project_json` in `crates/gcode/src/config/tests.rs` rather than stuffing parent keys into tracked metadata. `mark_as_worktree` in grant tests writes the sidecar (that helper exists to produce an overlay project id).

`code_overlay_project_id` stays a wrapper around `read_isolation_marker`; `overlay_requires_a_complete_foreign_isolation_marker` writes the sidecar.

Rebuild and reinstall `gcode` via a new inode after the crate change. No schema/migration carriers.

**Acceptance:**

- 2.1.1 - `read_isolation_marker` reads the sidecar from Constraints and ignores parent keys in tracked project metadata. symbol: `read_isolation_marker`.
- 2.1.2 - Complete sidecar still resolves `IsolatedOverlay`. test: `crates/gcode/src/config/tests.rs::isolated_marker_with_parent_metadata_resolves_overlay_scope`.
- 2.1.3 - Incomplete sidecar still XOR-rejects naming the sidecar path; leftover parent keys in tracked metadata do not. test: `crates/gcode/src/config/tests.rs::isolated_marker_without_complete_parent_metadata_is_rejected`.
- 2.1.4 - Grant worktree helper marks overlay via sidecar. symbol: `mark_as_worktree`.

## P3: Document the sidecar
`kind: framing`

**Goal**: Operators and gcode docs describe the sidecar from Constraints, not parent keys in committed project metadata.

### 3.1 Update isolation-marker docs [category: docs] (depends: 2.1)
`kind: deliverable`

Targets:
- `docs/guides/configuration.md`
- `docs/guides/gcode-user-guide.md`
- `docs/guides/gcode-development-guide.md`

`docs/guides/configuration.md` already says parent keys are stripped from committed project metadata and created in isolated checkouts. Document that isolated checkouts carry the gitignored sidecar from Constraints instead of rewriting tracked metadata. Keep the strip-on-update sentence.

gcode user/dev guides: IsolatedRoot / overlay fire when the sidecar carries both parent fields, not when tracked metadata does. Linked worktrees without a sidecar stay `LinkedWorktree` + `Single` (manual `git worktree add` without Gobby). Gobby-managed create/repair always writes the sidecar, so they stay overlay.

Do not invent a CLI backfill. Next `create_worktree` / `repair_isolation_environment` / agent spawn writes the sidecar for that checkout. Existing dirty trees become clean when repair checks tracked metadata back out from HEAD.

**Acceptance:**

- 3.1.1 - Configuration guide documents the sidecar and still forbids committing parent keys on the parent checkout. behavior: "sidecar contract" in `docs/guides/configuration.md`.
- 3.1.2 - gcode user guide overlay/isolation marker section names the sidecar from Constraints. behavior: "sidecar overlay marker" in `docs/guides/gcode-user-guide.md`.
- 3.1.3 - gcode development guide identity table uses the sidecar as the IsolationMarker source. behavior: "sidecar IsolationMarker" in `docs/guides/gcode-development-guide.md`.

## V1 End-to-end check
`kind: verification`

After 1.1–3.1 in one PR, with rebuilt `gcode`:

1. `create_worktree` on this repo (`use_local=true`, base `0.5.0`) → worktree `git status --short` empty except ignored sidecar; `project.json` mode still 0755.
2. `gcode` status/index inside that worktree uses `IsolatedOverlay` (parent = main checkout id).
3. Commit one file in the worktree; `delete_worktree` without `force` succeeds after merge (or after the focused unit that only needs the dirty-tree precheck).
4. `get_workflow_project_path` from the worktree still returns the main checkout.
5. Focused tests: `tests/mcp_proxy/tools/test_worktrees_create.py`, `tests/utils/test_project_context.py`, `tests/worktrees/test_parent_project_path.py`, `tests/agents/test_isolation_project_json.py`, `tests/code_index/test_eligibility.py`, gcode `config::tests` / `gcore` project tests.

## C1 Out of scope
`kind: framing`

- Changing `LinkedWorktree` from `ProjectIndexScope::Single` to overlay for manual worktrees that never got a sidecar.
- Registry-row parent metadata.
- Making `.gobby/project.json` itself gitignored.
- A bulk CLI to backfill every registered worktree (repair on next use is enough).
- Clone delete force-path beyond sharing the same writer (clone tests in 1.1 cover the sidecar write).
