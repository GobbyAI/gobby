# Shared-checkout coordination and managed workspace adoption

**Plan ID:** `shared-checkout-coordination`

## Summary
`kind: framing`

Add same-checkout dirty-file edit locking, nullable detached-workspace representation, adopt-on-delete for unregistered worktrees/clones, reverse reconciliation, and rules blocking unmanaged Git workspace commands.

## P1: Implementation Changes
`kind: framing`

### 1.1 Add dirty foreign-file edit lock [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/commit_guard.py::*` — scope-reason: add shared ownership conflict inspection and reason formatting
- `src/gobby/workflows/hooks.py::*` — scope-reason: seed the memoized dirty-file evaluation context
- `src/gobby/workflows/engine/core.py::*` — scope-reason: expose the rule evaluation default
- `src/gobby/install/shared/workflows/rules/task-enforcement/block-cross-session-foreign-dirty-edit.yaml::*` — scope-reason: install the complete task-enforcement rule template
- `src/gobby/install/shared/workflows/rules/CLAUDE.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: refresh every bundled template hash after rule changes
- `tests/workflows/test_commit_guard.py::*` — scope-reason: cover dirty ownership conflict edge cases
- `tests/workflows/test_task_enforcement_rules.py::*` — scope-reason: cover installed rule shape and execution

   - Add `foreign_dirty_edit_conflict()` using canonical mutation paths, tool-cwd-relative resolution, existing same-checkout foreign ownership lookup, and the memoized dirty-file set.
   - Inspect only canonical repository mutations. Fail open on inspection errors; retain the fail-closed commit guard.
   - Seed and populate the evaluation context, then add the priority-26 task-enforcement rule.
   - Block reasons identify owners and prescribe: buildable WIP commit via `gobby-agents.send_message`, migration through `gobby-worktrees`, or stale-owner `claim_task(force=true)`.
   - Add the rule name to `TASK_ENFORCEMENT_RULES`, update the derived documentation count from 19 to 20, and regenerate `bundled_content_manifest.json`.

**Acceptance:**

- 1.1.1 - Canonical edits to dirty foreign-owned files in the same checkout are blocked with actionable owner and recovery guidance, while the specified allow and fail-open cases remain permitted. behavior: "dirty foreign-file edit lock".

### 1.2 Support nullable isolation branches across storage and UI [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/baseline.sql`
- `src/gobby/storage/worktrees.py::*` — scope-reason: represent and validate detached worktrees across storage operations
- `src/gobby/storage/clones.py::*` — scope-reason: represent and validate detached clones across storage operations
- `web/src/components/agents/IsolationTargetSelector.tsx::*` — scope-reason: render and select detached isolation targets
- `web/src/components/chat/BranchIndicator.tsx::*` — scope-reason: render detached branch state
- `web/src/hooks/useSourceControl.ts::*` — scope-reason: accept nullable branch payloads

   - Add append-only baseline statements dropping `NOT NULL` from `worktrees.branch_name` and `clones.branch_name`.
   - Complete the baseline-375 refresh contract: predecessor checksum and fixture, refresh-prefix allowlist, predecessor reapply test, baseline checksum, PostgreSQL catalog manifest, release `gdaemon`, packaged schema identity, and pinned schema-contract values.
   - Change storage models and `LocalWorktreeManager.create`/`LocalCloneManager.create` to accept `str | None`.
   - Keep public `create_worktree` and `create_clone` MCP schemas and function contracts branch-required; add regression tests preventing managed branchless creation.
   - Reject branchless records from branch-dependent worktree task-link/sync/merge/mark-merged operations and clone sync/merge operations. Lookup, claim/release, reconciliation, and deletion remain available.
   - Change affected web types to `string | null`. Render the exact fallback `detached`, exclude null values from branch-name sets, and preserve worktree selection by ID/path.
   - Add detached rendering tests for `IsolationTargetSelector` and `BranchIndicator`, null-response coverage for `useSourceControl`, plus frontend type-check and tests.

**Acceptance:**

- 1.2.1 - Detached worktrees and clones persist and render with nullable branches, managed creation remains branch-required, and branch-dependent operations reject detached records. behavior: "nullable isolation branch contract".

### 1.3 Adopt unregistered worktrees on delete [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/worktrees/_lifecycle.py::*` — scope-reason: inspect, register, and delete path-addressed worktrees
- `src/gobby/storage/worktrees.py::*` — scope-reason: register adopted worktrees and collapse races
- `src/gobby/worktrees/git/_lifecycle.py::*` — scope-reason: inspect linked worktree metadata

   - Add shared inspection/registration logic that canonicalizes the path and verifies an existing, non-bare, non-prunable linked worktree.
   - Reject the primary checkout and paths registered on another machine.
   - Store Git’s real branch or `NULL`; re-read same-path insertion races and propagate unrelated uniqueness conflicts.
   - Emit `worktree_adopted`.
   - Change MCP `delete_worktree` to accept exactly one of `worktree_id` or `worktree_path`, retaining optional `project_path` for authoritative context resolution.
   - Preserve unknown-ID idempotency. Invalid or unadoptable paths return errors and never `already_deleted`.

**Acceptance:**

- 1.3.1 - Path-addressed deletion adopts eligible linked worktrees with real branch metadata or a null detached branch, rejects unsafe paths, and preserves ID retry semantics. behavior: "worktree adopt-on-delete contract".

### 1.4 Adopt unregistered clones on delete [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/clones/git.py::*` — scope-reason: expose clone containment and clone-local Git inspection
- `src/gobby/mcp_proxy/tools/_clones_operations.py::*` — scope-reason: adopt and delete path-addressed clones
- `src/gobby/storage/clones.py::*` — scope-reason: revive and register adopted clone rows

   - Expose a shared canonical containment check from `CloneGitManager`.
   - Resolve the project through `LocalProjectManager` and require the canonical clone path’s project directory to equal `CLONES_ROOT/<resolved-project-name>`.
   - Require a valid Git status with branch or commit; store the real branch or `NULL`.
   - Read `origin` from the clone itself by allowing `get_remote_url()` to run with the clone path as its Git cwd.
   - Add `get_by_path_any_status()` and reject existing rows belonging to another project.
   - Revive `CLEANUP` rows with actual branch/remote metadata while clearing task/session ownership, cleanup timestamps, sync metadata, and stale lifecycle state. Treat `DELETING` rows as registered retry state without resetting them.
   - Change MCP `delete_clone` to accept exactly one of `clone_id` or `clone_path`, then retain the deleting/filesystem/row-deletion lifecycle.
   - Introduce no clone-adoption event; the clone subsystem has no event facility.

**Acceptance:**

- 1.4.1 - Path-addressed deletion adopts eligible clones inside the resolved project directory, uses clone-local metadata, revives cleanup rows safely, and preserves deleting retries. behavior: "clone adopt-on-delete contract".

### 1.5 Reconcile unregistered isolation workspaces [category: code] (depends: 1.3, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/worktrees/lock.py`
- `src/gobby/worktrees/reconciliation.py`
- `src/gobby/background/isolation_cleanup.py`

   - Add `IsolationRegistryReconciliation(machine_id)` at priority 550 with advisory key `isolation_registry_reconciliation:<machine_id>`.
   - Document that future nested typed locks acquired during reconciliation must have priority greater than 550.
   - Extend the hourly isolation loop under that per-machine lock.
   - Scan registered projects with repository paths; skip primary, bare, prunable, `_orphaned*`, and `_migrated*` worktrees.
   - Scan immediate clone directories only beneath registered projects’ expected `CLONES_ROOT/<project-name>` directories.
   - Keep Git/filesystem inspection in `asyncio.to_thread` and storage operations behind `run_db`, using inspection/registration stages shared with direct adoption.
   - Collapse races through unique constraints and log one summary when a cycle adopts anything.

**Acceptance:**

- 1.5.1 - Hourly per-machine reconciliation adopts each eligible stray workspace once, skips unsafe and unregistered locations, and remains race-safe. behavior: "reverse isolation registry reconciliation".

### 1.6 Block unmanaged Git isolation commands [category: code] (depends: 1.3, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/worker-safety/block-git-worktree-mutations.yaml::*` — scope-reason: define both worker and interactive worktree mutation rules
- `src/gobby/install/shared/workflows/rules/worker-safety/block-git-clone.yaml::*` — scope-reason: define both worker and interactive clone rules
- `src/gobby/install/shared/workflows/rules/CLAUDE.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: refresh every bundled template hash after rule changes
- `tests/workflows/test_worker_safety_rules.py::*` — scope-reason: cover Git command parsing, audience gating, and documentation counts

   - Add worker and interactive rule pairs at priorities 50/55.
   - Block `git worktree add|remove|move|prune|repair`, including supported global Git options; allow `list`, `lock`, and `unlock`.
   - Block `git clone`.
   - Point reasons to `gobby-worktrees.create_worktree`, path-capable `delete_worktree`, and `gobby-clones.create_clone`. Interactive rules append the standard user-permission-to-disable wording.
   - Update worker-safety documentation from the actual 52 rules to 56.
   - Add a YAML-aware test that derives task-enforcement and worker-safety counts from each file’s `rules` mapping and compares them with the documentation table.
   - Regenerate `bundled_content_manifest.json`; it contains hashes for rule templates and remains part of installation-integrity validation.

**Acceptance:**

- 1.6.1 - Worker and interactive rules block unmanaged worktree mutations and cloning across supported Git global options, preserve allowed subcommands, and keep derived documentation and bundled hashes current. behavior: "managed Git isolation command enforcement".

## Public Contracts
`kind: framing`

- `delete_worktree`: optional `worktree_id`, optional `worktree_path`, existing optional `project_path`; JSON Schema `oneOf` plus runtime validation requires exactly one identifier.
- `delete_clone`: optional `clone_id`, optional `clone_path`; same exact-one contract.
- Read APIs may expose `"branch_name": null`.
- Managed creation APIs continue requiring real branch names.
- HTTP worktree deletion remains ID-based and unchanged.
- Rule templates continue syncing into the database through normal startup installation.

## Test Plan
`kind: framing`

- Dirty lock: foreign dirty block; foreign clean, own-session, other-checkout, unattributed, and completed-session allows; Edit and normalized Bash; outside-checkout skip; noncanonical mutation avoids status inspection; inspection failure allows.
- Schema/storage: fresh and predecessor baseline application, nullable catalog contract, generated manifest/identity freshness, optional storage typing, branch-required public creation, and branch-dependent operation failures.
- Web: detached labels, branch-set filtering, nullable source-control payloads, `npm run type-check`, focused Vitest, then full `npm test`.
- Worktrees: branch-backed and detached adopt-then-delete, primary/non-worktree/other-machine rejection, existing-path reuse, race handling, XOR schema, and unknown-ID retry behavior.
- Clones: branch-backed and detached adoption, project-directory mismatch, clone-origin discovery, cleanup tombstone revival, deleting-state preservation, invalid Git directory, out-of-root rejection, race handling, and XOR schema.
- Reconciliation: each stray type adopted once, second pass no-op, unregistered/hidden projects ignored, per-machine advisory key, and uniqueness races.
- Rules: regex hits/misses, Git global options, allowed worktree subcommands, audience gating, explicit task-rule list, derived documentation counts, startup sync/shape, and bundled-content manifest freshness.
- Run focused pytest with `GOBBY_TEST_PROTECT=1` against an isolated database; never run the full pytest suite.
- Run Ruff check/format-check, full `mypy src/`, affected test-quality/type audits, and `gobby-core` schema tests with PostgreSQL support.
- Perform the four requested end-to-end spot checks.

## Delivery
`kind: framing`

Create six `code/backend` feature leaves before code edits:

| Leaf | Dependency |
|---|---|
| Dirty foreign-file edit lock | — |
| Nullable isolation branches across schema/storage/UI | — |
| Worktree adopt-on-delete | Nullable branches |
| Clone adopt-on-delete | Nullable branches |
| Reverse reconciliation | Worktree and clone adoption |
| Naked Git command rules | Worktree and clone adoption |

Claim and land sequentially. Before each edit, verify path ownership and projected production-file line counts. Preserve current unrelated shared-worktree edits. Each leaf receives focused validation, a task-linked `[gobby-#NNNNN] <type>: <summary>` commit, and `close_task(..., commit_sha=...)`. Run aggregate validation before closing the final leaf.

Accepted limits remain: Bash protection follows canonical mutation classification; the clean-file check/edit race remains commit-guard-backed; stale ownership uses existing force-claim semantics. No stash, copy-out parking, compatibility layer, or additional escape mechanism is introduced.
