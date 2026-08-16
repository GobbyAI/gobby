Plan artifact: `.gobby/plans/project-checkout-identity.md`

# Path-Independent Project Identity

**Plan ID:** project-checkout-identity

## Overview
`kind: framing`

Replace global `projects.repo_path` with machine-owned `project_checkouts` while `.gobby/project.json.id` stays the only project UUID. Downstream order remains `#19651 → #18902 → #17678 → #19664`. This file supersedes the sketch at `.gobby/plans/path-independent-project-identity.md`.

## Constraints
`kind: framing`

- Preparation, not a code leaf: before `gobby build` or expansion, the coordinator promotes `#19651` to an epic under `#17435`; wires `#19651 → #18902 → #17678 → #19664`; writes the C1 grant and `resolve_tool_session` obligation onto `#18902` with validation criteria and `deferred-from:project-checkout-identity:C1`; writes D1.1 onto `#17678` with validation criteria, `deferred-from:project-checkout-identity:D1`, and `cited-parent:#19670`; writes `out-of-scope-for:#19651` onto `#19670` and verifies `#19670` is not in `#19651`'s dependency closure; and replaces `#19651`'s stale `tests/projects/test_stable_identity.py` validation command with the focused checkout tests named in this plan. After those task-graph mutations, register `.gobby/plans/project-checkout-identity.md` as the active implementation plan `plan_id` `project-checkout-identity` with `root_task_ref` `#19651`; generate `.gobby/plans/project-checkout-identity.coverage-ledger.yaml`; and verify `get_plan` by plan id and by `#19651` resolve to the same active row and that the ledger validates. Verify type, parentage, edges, labels, cited-parent routing, validation criteria, registry row, and ledger then. Do not implement those mutations in P1–P6.
- Trusted hub is the global project namespace. Hosted tenancy is out of scope.
- `project_id` is the only repository UUID. Paths are never project identity. For an existing marker ID, `projects.name` is authoritative; a local marker `name` is machine-local metadata and never overwrites the database name.
- One ordinary primary checkout per `(machine_id, project_id)`. Managed worktrees and clones are unlimited overlays and are never inserted into `project_checkouts`.
- Checkout-free sentinel IDs are `_global`, `_orphaned`, `_migrated`, and `_personal` (`GLOBAL_PROJECT_ID`, `ORPHANED_PROJECT_ID`, `MIGRATED_PROJECT_ID`, `PERSONAL_PROJECT_ID`). `register`, `rebind`, `require_root`, CLI mutations, HTTP register/rebind, hook ingress, and cutover refuse every member with `CheckoutSentinelRejectedError`. Present sentinel project JSON and `GET /checkouts` return `checkout: null` and do not call `require_root`. The named `gobby` project is an ordinary repo, not a sentinel. Hub-owned file location for the wiki home, `_personal` files, and `USER.md` is `#20238`, not this plan.
- Hub stores `root_path` as an opaque string. Local daemons normalize and touch the filesystem. Same-machine uniqueness is exact-string equality, not filesystem equality.
- There is no shipped compatibility surface. New code never reads or writes `projects.repo_path`. The column stays in baseline 375 only as unread source data until the local `project-checkout-cutover` campaign copies it into `project_checkouts` and drops it. No dual-read, dual-write, SQL `COALESCE`, or reusable legacy-mapping interface. No automatic predecessor-receipt refresh.
- Do not restart the daily daemon on a mid-epic commit. Work stays in worktrees until P6 completes.
- `hook_manager.py` is 864 lines. Do not add registration logic there. Keep `HookManager._ensure_project_in_db` as a delegate.
- Every touched hand-maintained production source stays under 1,000 lines.
- Validation is `GOBBY_TEST_PROTECT=1` plus focused pytest, Ruff, Mypy, gcode, and gcore schema-contract tests. Frontend leaf 5.1 also runs focused Vitest plus `npm --prefix web run type-check` and `npm --prefix web run lint`. Do not run the full suite.
- `#18902` must consume the grant and `resolve_tool_session` shape in C1, including `projects SELECT (id, name, deleted_at)`. Do not leave `projects.repo_path` in gcode’s grant list.

## C1: Public contracts
`kind: framing`

```text
projects.id  <── project_checkouts.project_id
machines.id  <── project_checkouts.machine_id
                 unique (machine_id, root_path)
                 primary key (machine_id, project_id)
```

- `project_checkouts`: `machine_id uuid NOT NULL REFERENCES machines(id) ON DELETE CASCADE`, `project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE`, `root_path text NOT NULL`, `created_at timestamp with time zone DEFAULT now() NOT NULL`, `updated_at timestamp with time zone DEFAULT now() NOT NULL`.
- Typed errors: `MissingMachineContextError`, `CheckoutNotFoundError`, `CheckoutConflictError`, `CheckoutRootTakenError`, `MarkerMismatchError`, `NameAttachRejectedError`, `AmbiguousProjectRefError`, `SoftDeletedProjectRejectedError`, `OverlayRegistrationRejectedError`, `CheckoutSentinelRejectedError`.
- Checkout-free sentinel IDs: `ORPHANED_PROJECT_ID`, `MIGRATED_PROJECT_ID` (`00000000-0000-0000-0000-000000000001` if the Python constant is missing), `GLOBAL_PROJECT_ID`, `PERSONAL_PROJECT_ID`. One `CHECKOUT_FREE_PROJECT_IDS` frozenset.
- `LocalProjectCheckoutManager`: `get`, `list_for_machine`, `register` (idempotent same-root; returns `(checkout, created)` from the same INSERT/conflict transaction), `rebind`. Filesystem-free. No `require_root` method and no unused `list_for_project`. `register` and `rebind` recheck machine-qualified overlay membership inside the same write transaction after caller-side `validate_checkout_root`; they do not add an advisory-lock subsystem. `rebind` has three branches: absent row inserts after validation and preserves active index state only when none exists or its recorded root matches the inserted checkout — a different recorded root clears that machine’s active project and file states in the same transaction; same root returns the existing row with no timestamp mutation and no invalidation; different root updates the row and deletes only that machine’s active index state. Concurrent absent-row `rebind` attempts INSERT first; on primary-key conflict it re-reads the checkout row `FOR UPDATE` and executes the same-root or different-root branch, translating root-ownership conflicts. `CheckoutRootTakenError` leaves the row unchanged.
- `require_root(db, project_id, machine_id) -> str` and `resolve_operation_root(db, project_id, machine_id, *, overlay_path=None) -> str` are module functions in `src/gobby/storage/project_checkouts.py`. Missing `machine_id` raises `MissingMachineContextError` and does not fall back to the local daemon. A provided `machine_id` is then checked with `require_local_machine_id(machine_id, resource_kind="project_checkout", resource_id=project_id)` before any checkout or overlay lookup. Foreign-machine refusal happens before filesystem access. `LocalProjectCheckoutManager.get` and `list_for_machine` remain filesystem-free opaque inspection. `overlay_path=None` is the only primary-checkout fallback and raises `CheckoutNotFoundError` when that row is missing. A checkout-free sentinel ID raises `CheckoutSentinelRejectedError`. A non-null `overlay_path` wins only when that path is a worktree or clone registered on `machine_id` for `project_id`, and that overlay is returned even when no primary checkout exists; every non-null unregistered, wrong-project, or foreign-machine overlay raises a typed refusal.
- `validate_checkout_root(db, *, project_id, machine_id, candidate_path, expected_marker_id)` is created in § 1.3. Verified local machine, marker/project agreement, platform-local root normalization, overlay refusal. Returns the opaque root string. Every checkout-establishing write (`create`, `ensure_exists`, `update`, `register`, `rebind`, and the § 6.1 campaign) calls `require_local_machine_id(provided_machine_id, resource_kind="project_checkout", resource_id=project_id)` first, then `validate_checkout_root` with that returned `machine_id`, then the manager write. Foreign-machine rejection happens before filesystem access.
- Registration sources: `gobby init` on a non-overlay root, and authenticated local hook ingress on a non-overlay root. Each requires verified local `machine_id` via `require_local_machine_id(provided_machine_id, resource_kind="project_checkout", resource_id=project_id)`. Pass `provided_machine_id=None` when ingress has no claimed machine. `_personal` startup is not a registration source.
- `require_root` / `register` / `rebind` refuse every `CHECKOUT_FREE_PROJECT_IDS` member with `CheckoutSentinelRejectedError`.
- CLI: `gobby projects rebind PROJECT_REF [PATH]`; remove `gobby projects update --repo-path`. `gobby projects rename` updates `projects.name` globally and does not require a local checkout; when the calling daemon has a checkout, it refreshes only that local marker through the § 2.1 expected-id helper after the database commit.
- HTTP:
  - logical project JSON has no `repo_path`
  - `checkout: {machine_id, root_path} | null` is the calling daemon’s row
  - `GET /api/projects/{project_id}/checkouts` returns `{checkout: {machine_id, root_path} | null}` for the calling daemon only. Missing project is 404. A present ordinary project, checkout-free sentinel, or other present project with no local checkout is 200 and `checkout: null`
  - `POST /api/projects/{project_id}/checkouts` body `{root_path: string}`; success `{checkout: {machine_id, root_path}}`. Call `require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)` and take HTTP 201/200 from `register`'s `created` flag (201 first insert, 200 idempotent same-root). Unavailable local machine identity is 409 `MissingMachineContextError`. Soft-deleted project is 409 `SoftDeletedProjectRejectedError` without restore
  - `POST /api/projects/{project_id}/checkouts/{machine_id}/rebind` body `{root_path: string}`; success 200 `{checkout: {machine_id, root_path}}`. Path `machine_id` must equal `require_local_machine_id(path_machine_id, resource_kind="project_checkout", resource_id=project_id)` or 409. Unavailable local machine identity is 409 `MissingMachineContextError`. Soft-deleted project is allowed and preserves `deleted_at`
  - Shared mutation errors: 404 missing project; 409 foreign machine, `MissingMachineContextError`, `CheckoutConflictError`, `CheckoutRootTakenError`, `OverlayRegistrationRejectedError`, `MarkerMismatchError`, `CheckoutSentinelRejectedError`; 400 relative/`~`/nonexistent path
- Grant shape left for `#18902`:
  - `projects`: `SELECT (id, name, deleted_at)` only
  - `project_checkouts`: `SELECT, UPDATE (machine_id, project_id, root_path)` scoped to the grant machine; UPDATE is lock-only (`SELECT ... FOR SHARE`). Capability write policies stay absent so checkout mutation remains denied.
  - `gobby_agent_auth.resolve_tool_session(p_session_id UUID) RETURNS (session_id, project_id, machine_id, root_path)` `LEFT JOIN`s each eligible session to `project_checkouts` on `sessions.machine_id` + `sessions.project_id` only, keeps `COALESCE(session.status, 'active') NOT IN ('expired', 'deleted')`, and permits a null `root_path` when no primary checkout exists. Expired or deleted sessions still return no row. No `projects.repo_path` fallback.

## D1: Repository content versus index-view identity
`kind: deferred`

Canonical repository content identity separate from index-view identity stays in `#17678`. This plan only keeps `code_indexed_project_states.root_path` as view metadata, requires primary-view agreement with the registered checkout, and deletes only the rebound `(machine_id, project_id)` active project/file states. Typed-deferral routing for D1 is the cited-parent path: `#17678` carries `cited-parent:#19670`, and `#19670` carries `out-of-scope-for:#19651`. The `#19651 → #18902 → #17678 → #19664` chain stays; it does not put `#17678` in `#19651`'s dependency closure.

```yaml
deferral:
  task_ref: "#17678"
  reason: "Content-identity versus index-view identity is the repository-intelligence redesign, not checkout persistence."
  owner: "repository-intelligence"
  original_acceptance_items:
    - D1.1
```

## P1: Checkout persistence and resolution
`kind: framing`

**Goal:** Persist machine-owned checkouts in gcore DDL and make `(project_id, machine_id)` the only Python/SQL resolution key. `projects.repo_path` is unread campaign input until P6 copies and drops it.

### 1.1 Add project_checkouts to baseline 375 [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate the catalog after adding project_checkouts and grant changes
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: regenerate baseline identity constants
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pin the new relation; projects.repo_path remains until 6.1
- `crates/gcore/tests/catalog_manifest_freshness.rs::*` — scope-reason: cover the new table in freshness checks
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: synchronize Python expected identity
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: add project_checkouts machine-scoped grants and projects.deleted_at SELECT
- `tests/storage/test_postgres_agent_authorization.py::*` — scope-reason: two-machine checkout grant isolation and live deleted_at predicate against the capability role
- `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`

Add this table in the alphabetical table section of `crates/gcore/assets/schema/baseline.sql`, with PK, unique root, and cascading FKs in the existing constraint sections:

```sql
CREATE TABLE project_checkouts (
    machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    root_path text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY project_checkouts FORCE ROW LEVEL SECURITY;
```

Add in the existing constraint sections: PK `(machine_id, project_id)`, unique `(machine_id, root_path)` (exact-string), and `ON DELETE CASCADE` FKs to `machines(id)` and `projects(id)`. This task does not change `resolve_tool_session` and does not drop `projects.repo_path`. The Python dataclass and `LocalProjectCheckoutManager` CRUD belong to § 1.2; this leaf owns DDL, generated identities, grants, and schema authorization only. Extend the privilege-manifest exact relation set, columns, and `machine_id` scope for `project_checkouts`.

Add `project_checkouts` to the existing `$rls$` relation inventory in `crates/gcore/assets/schema/baseline.sql` and grant `gobby_daemon_runtime` table CRUD in the alphabetical grant section. Fresh schema must install `gobby_daemon_runtime_access` (`USING (TRUE) WITH CHECK (TRUE)`), `gobby_migration_owner_access` (`USING (TRUE) WITH CHECK (TRUE)`), and a capability SELECT policy constrained by `project_id = gobby_agent_auth.current_project_id() AND machine_id = gobby_agent_auth.current_machine_id()`. Grant `gobby_gcode_capability` `SELECT, UPDATE` on `project_checkouts (machine_id, project_id, root_path)` scoped to the grant machine so `SELECT ... FOR SHARE` is legal. Do not add `project_checkouts` to the `$gcode_rls$` write-policy inventory and do not add capability INSERT/UPDATE/DELETE policies: FORCE RLS plus SELECT-only policies keep mutation denied. Do not add a SECURITY DEFINER lock helper. § 6.1 campaign bootstrap installs the same policies and lock-only UPDATE grant before any checkout insert.

Grants and RLS: the daemon role can CRUD every local checkout row; machine-scoped capabilities select only checkout rows for their `project_id` and `machine_id`, may `SELECT ... FOR SHARE` those rows, and cannot INSERT/UPDATE/DELETE them. Existing `projects.repo_path` grants stay until 6.1 removes them with the column. Add `GRANT SELECT(deleted_at) ON TABLE projects TO gobby_gcode_capability` in the alphabetical grant section and add `deleted_at` to the privilege-manifest `projects` columns so the 4.1 active-name `WHERE projects.deleted_at IS NULL` predicate is legal under the column-scoped grant. Keep `SELECT (id, name)` and the existing `repo_path` grant. Live capability authorization must execute that predicate as `gobby_gcode_capability`; a missing column grant is a failure. Extend the isolated authorization fixture with two machines and two checkout rows; each issued capability sees only its machine row, can `FOR SHARE` that row, and is refused on checkout mutation; daemon-role CRUD succeeds independently of capability isolation. Do not assert the P6 column drop here.

Keep baseline version 375 and regenerate catalog checksum plus expected identity. No numbered migration.

**Acceptance:**

- 1.1.1 - Baseline 375 creates `project_checkouts` with the stated columns, timestamp defaults, PK, unique root, cascading FKs, and forced RLS. file: `crates/gcore/assets/schema/baseline.sql`.
- 1.1.2 - Machine-scoped grants list `project_checkouts` SELECT and UPDATE `(machine_id, project_id, root_path)` scoped to the grant machine. file: `crates/gcode/security/managed_postgres_privileges.json`.
- 1.1.3 - Two issued capabilities on different machines each see only their checkout row, can `SELECT ... FOR SHARE` that row, and cannot INSERT/UPDATE/DELETE checkout rows. test: `tests/storage/test_postgres_agent_authorization.py`.
- 1.1.4 - Privilege-manifest parity includes `project_checkouts` SELECT and UPDATE `(machine_id, project_id, root_path)` scoped to `machine_id`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.
- 1.1.5 - Regenerated `catalog.manifest.json`, `assets.rs` identities, `schema_expected_identity.json`, catalog freshness, and schema-contract tests match staged 375 with `project_checkouts` present and `projects.repo_path` still in place. file: `crates/gcore/assets/schema/catalog.manifest.json`. test: `crates/gcore/tests/catalog_manifest_freshness.rs`. test: `crates/gcore/tests/schema_contract.rs`.
- 1.1.6 - Fresh schema installs daemon-runtime, migration-owner, and project-and-machine capability SELECT policies plus the lock-only UPDATE grant on `project_checkouts`; daemon-role CRUD succeeds; two issued capabilities remain machine-isolated, can `FOR SHARE` their row, and cannot mutate checkout rows. test: `tests/storage/test_postgres_agent_authorization.py`.
- 1.1.7 - Capability `projects` grants include `SELECT (id, name, deleted_at)` plus the existing `repo_path` grant; privilege-manifest parity lists those columns; live capability authorization executes `projects.deleted_at IS NULL` as `gobby_gcode_capability`. file: `crates/gcode/security/managed_postgres_privileges.json`. test: `tests/storage/test_postgres_agent_authorization.py`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.

### 1.2 Add checkout storage and typed errors [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/project_checkouts.py`
- `src/gobby/storage/projects.py::*` — scope-reason: define `MIGRATED_PROJECT_ID` and `CHECKOUT_FREE_PROJECT_IDS` beside the existing sentinel constants
- `tests/storage/test_project_checkouts.py`

Create `LocalProjectCheckoutManager` in a new module. Keep it off `LocalProjectManager`.

```python
@dataclass(frozen=True)
class ProjectCheckout:
    machine_id: str
    project_id: str
    root_path: str
    created_at: datetime
    updated_at: datetime

class LocalProjectCheckoutManager:
    def get(self, machine_id: str, project_id: str) -> ProjectCheckout | None: ...
    def list_for_machine(self, machine_id: str) -> list[ProjectCheckout]: ...
    def register(
        self, machine_id: str, project_id: str, root_path: str
    ) -> tuple[ProjectCheckout, bool]: ...
    def rebind(self, machine_id: str, project_id: str, root_path: str) -> ProjectCheckout: ...
```

`register` is idempotent when `(machine_id, project_id, root_path)` already matches. It returns `(checkout, created)` from the same INSERT/conflict transaction: `created=True` only for the inserting winner. Same machine + same project + different `root_path` raises `CheckoutConflictError` without mutation; callers that intend to move must call `rebind`. Same machine + same `root_path` + different project raises `CheckoutRootTakenError`. `register` and `rebind` recheck machine-qualified overlay membership inside the same write transaction; an overlay inserted after caller-side `validate_checkout_root` is `OverlayRegistrationRejectedError`. Do not add an advisory-lock subsystem. `create`, `ensure_exists`, `update`, hook ingress, CLI, HTTP, and the § 6.1 campaign persist only through these manager methods. `rebind` is the retry-safe repair mutation: absent `(machine_id, project_id)` inserts the validated root and, in the same transaction, preserves active index state only when none exists or its recorded root matches the inserted checkout; a different recorded root clears that machine’s active project and file states. Same root returns the existing row without changing `updated_at` or invalidating; different root updates the row. Concurrent absent-row `rebind` attempts INSERT first; on primary-key conflict it re-reads the checkout row `FOR UPDATE` and executes the same-root or different-root branch. Index deletion for the different-root branch and for mismatched absent-row state is owned by § 4.2. Root ownership conflicts raise `CheckoutRootTakenError` without mutation. Hub stores the supplied string unchanged: a Unix root and a Windows-style root (`C:\work\repo`) round-trip without server reinterpretation or separator rewriting. `register` and `rebind` raise `CheckoutSentinelRejectedError` for every `CHECKOUT_FREE_PROJECT_IDS` member. `require_root` is the § 1.3 module function, not a manager method, and raises the same sentinel error. Define `MIGRATED_PROJECT_ID = "00000000-0000-0000-0000-000000000001"` and `CHECKOUT_FREE_PROJECT_IDS` in `src/gobby/storage/projects.py` next to `ORPHANED_PROJECT_ID`, `PERSONAL_PROJECT_ID`, and `GLOBAL_PROJECT_ID`. Pin all four IDs in `tests/storage/test_project_checkouts.py`.

**Acceptance:**

- 1.2.1 - Lookup, list, idempotent same-root register, conflict, root-taken, rebind, and sentinel refusal behave as specified. test: `tests/storage/test_project_checkouts.py`.
- 1.2.2 - Unix and Windows-style `root_path` strings store and retrieve unchanged with no server-side path interpretation. test: `tests/storage/test_project_checkouts.py`.
- 1.2.3 - `rebind` inserts when absent, no-ops the same root without timestamp mutation, updates a different root, and raises `CheckoutRootTakenError` without mutation when another project owns the root. Absent-row insert preserves matching or empty active index state and leaves mismatched-root cleanup to § 4.2. test: `tests/storage/test_project_checkouts.py`.
- 1.2.4 - `register` returns `(checkout, created)` from one INSERT/conflict transaction, and `CHECKOUT_FREE_PROJECT_IDS` pins `ORPHANED_PROJECT_ID`, `MIGRATED_PROJECT_ID`, `GLOBAL_PROJECT_ID`, and `PERSONAL_PROJECT_ID`. test: `tests/storage/test_project_checkouts.py`.
- 1.2.5 - `register` and `rebind` recheck machine-qualified overlay membership in the same transaction and refuse an overlay inserted after `validate_checkout_root`. test: `tests/storage/test_project_checkouts.py`.
- 1.2.6 - Concurrent absent-row `rebind` with equal roots produces one insert and typed same-root no-ops; different roots serialize through INSERT then `FOR UPDATE` and the loser takes the different-root branch. test: `tests/storage/test_project_checkouts.py`.

### 1.3 Resolve ordinary roots from project_checkouts [category: code] (depends: 1.2)
`kind: deliverable`

Targets:
- `src/gobby/storage/project_checkouts.py`
- `src/gobby/storage/projects.py::*` — scope-reason: drop Project.repo_path, write checkouts from create/ensure/update, and machine-qualify overlay detection
- `src/gobby/storage/workspace_machine_scope.py::require_local_machine_id`
- `src/gobby/utils/checkout_root.py`
- `tests/utils/test_checkout_root.py`
- `tests/conftest.py::sample_project`
- `tests/servers/conftest.py::*` — scope-reason: shared server fixture must use the isolated-machine helper instead of create(..., repo_path=)
- `tests/storage/test_project_checkouts.py`
- `tests/storage/test_storage_projects.py::*` — scope-reason: stop treating repo_path as a root and cover checkout-only writes

Add:

```python
def require_root(db, project_id: str, machine_id: str) -> str: ...
def resolve_operation_root(
    db, project_id: str, machine_id: str, *, overlay_path: str | None = None
) -> str: ...
```

Missing `machine_id` raises `MissingMachineContextError` and does not fall back to the local daemon. A provided `machine_id` is then checked with `require_local_machine_id(machine_id, resource_kind="project_checkout", resource_id=project_id)` before any checkout or overlay lookup or filesystem access. Foreign-machine refusal is that helper's typed mismatch. `LocalProjectCheckoutManager.get` and `list_for_machine` remain filesystem-free opaque inspection. `overlay_path=None` is the only primary-checkout fallback and raises `CheckoutNotFoundError` when that row is missing. A checkout-free sentinel ID raises `CheckoutSentinelRejectedError`. There is no `projects.repo_path` fallback. A non-null `overlay_path` wins only when that path is a worktree or clone registered on `machine_id` for `project_id`, and that overlay is returned even when no primary checkout exists. Every non-null unregistered, wrong-project, or foreign-machine overlay raises a typed refusal. Isolation copies of the project marker file are not checkouts.

Add `validate_checkout_root` in `src/gobby/utils/checkout_root.py`. Compose the existing marker reader and overlay detectors (`LocalProjectManager._is_registered_isolation_path` / `_guard_repo_path_write`). Callers call `require_local_machine_id(provided_machine_id, resource_kind="project_checkout", resource_id=project_id)` first — pass `None` when ingress has no claimed machine, and pass the HTTP path `machine_id` for rebind — then pass that returned id into `validate_checkout_root` as `machine_id`. Do not change that helper's signature. Foreign-machine rejection happens before filesystem access. The validator never expands `~` or relative paths. The client or CLI may expand before calling. Accept only a platform-local normalized absolute path and store that exact string. Refuse relative paths, unexpanded `~`, nonexistent paths, overlays, and marker mismatches with typed errors visible to the caller. Return the opaque root string. `LocalProjectCheckoutManager` stays SQL-only.

Make `_is_registered_isolation_path` machine-qualified: exact-path queries on `worktrees` and `clones` filter by `machine_id`. A foreign-machine overlay that shares the local candidate string is not a local overlay. Same-machine overlays still refuse checkout writes.

`LocalProjectManager.create` / `ensure_exists` / `update` take `machine_id` from `require_local_machine_id(provided_machine_id, resource_kind="project_checkout", resource_id=project_id)` first when they establish an ordinary root. They keep a filesystem path argument (not a `projects.repo_path` column write), call `validate_checkout_root` with that returned `machine_id`, then persist only through manager `register`. They do not write a checkout for an isolation/overlay path (`_guard_repo_path_write` / `_is_registered_isolation_path` remain the overlay detectors). `Project.to_dict` has no `repo_path`; tests that need a path call the resolver.

Add one deterministic isolated-machine helper used by `sample_project`, `tests/servers/conftest.py`, and the checkout resolver tests: isolated machine row, root marker, project, and checkout. Other `create(..., repo_path=)` callers migrate when their owning leaf changes the API, not as a suite-wide rewrite in this deliverable.

**Acceptance:**

- 1.3.1 - `require_root` returns the checkout row or raises the typed errors above, with no legacy-column fallback, and refuses a foreign `machine_id` before checkout lookup or filesystem access. test: `tests/storage/test_project_checkouts.py`.
- 1.3.2 - Ordinary create/ensure/update call `validate_checkout_root`, write only `project_checkouts`, and refuse overlay paths. test: `tests/storage/test_storage_projects.py`.
- 1.3.3 - `sample_project`, the shared servers fixture, and checkout resolver tests use the isolated-machine helper (marker, machine, project, checkout). test: `tests/conftest.py::sample_project`. test: `tests/servers/conftest.py`.
- 1.3.4 - `resolve_operation_root` with `overlay_path=None` uses the primary checkout and raises `CheckoutNotFoundError` when that row is missing; a valid registered local worktree or clone wins even when no primary checkout exists; every non-null unregistered, wrong-project, or foreign-machine overlay is a typed refusal; missing-machine and sentinel cases raise the typed errors above; a foreign `machine_id` is refused before overlay or checkout lookup. test: `tests/storage/test_project_checkouts.py`.
- 1.3.5 - `validate_checkout_root` never expands input; it rejects relative paths, unexpanded `~`, nonexistent paths, overlays, and marker mismatches, and accepts only a platform-local normalized absolute. test: `tests/utils/test_checkout_root.py`.
- 1.3.6 - Checkout writers call `require_local_machine_id(provided_machine_id, resource_kind="project_checkout", resource_id=project_id)` first, pass that returned id into `validate_checkout_root`, and cover local, missing, and foreign `provided_machine_id` cases with foreign rejection before filesystem access. test: `tests/utils/test_checkout_root.py`.
- 1.3.7 - A foreign-machine overlay that shares the local candidate string does not block a valid local checkout; a same-machine overlay still refuses. test: `tests/utils/test_checkout_root.py`.
- 1.3.8 - `require_root` and `resolve_operation_root` call `require_local_machine_id(machine_id, resource_kind="project_checkout", resource_id=project_id)` after the missing-`machine_id` check and before lookup; a foreign session machine is refused before filesystem access; `get` and `list_for_machine` still return opaque rows. test: `tests/storage/test_project_checkouts.py`.

## P2: Registration, CLI, and HTTP
`kind: framing`

**Goal:** Marker id is authoritative. Name-only attach dies. CLI and HTTP register or rebind a verified machine’s checkout.

### 2.1 Make init marker-authoritative [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `src/gobby/utils/project_init.py::*` — scope-reason: marker-authoritative initialize_project plus expected-id crash-durable marker refresh
- `src/gobby/storage/projects.py::*` — scope-reason: marker-authoritative get_or_create and ensure_exists stop name-attach and write checkouts
- `src/gobby/utils/checkout_root.py`
- `tests/utils/test_checkout_root.py`
- `tests/utils/test_utils_project_init.py::*` — scope-reason: replace name-attach and repo_path backfill coverage with the marker matrix

Call `require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)` first, then the § 1.3 `validate_checkout_root` with that returned `machine_id`, then `register` or `rebind`. The manager rechecks overlay membership in the same database transaction as the insert; do not add an advisory-lock subsystem for that overlay-versus-checkout check.

Crash recovery: write the stable marker UUID first, then create project plus checkout in one ID-targeted transaction. A failpoint retry after marker-only write must resume on the marker id, not hit `NameAttachRejectedError`.

Marker publication is crash-durable and no-clobber: write complete JSON to a sibling temporary file, `fsync` that file, install it with an atomic no-overwrite primitive (`link`/`rename` that fails if the marker file already exists — do not `os.replace` over a missing or existing file), then `fsync` the parent directory. The losing writer discards its temporary file and rereads the winner. Failpoints exist before the temp write, after file `fsync`, after install, and after directory `fsync`. Do not add an advisory-lock subsystem.

Concurrent no-marker init at one root: exclusive create-if-absent of that published marker. The losing writer rereads and adopts the complete winning marker payload (`id`, `name`, `created_at`) before its ID-targeted database transaction, or rejects a conflicting explicit name without mutation. Two same-root writers with different names produce one deterministic marker/project pair.

Concurrent no-marker init at two distinct non-overlay roots with the same unused name: each root may publish its own marker UUID. The active-name uniqueness constraint chooses one database winner. The loser confirms its marker UUID has no project row and that the name belongs to another UUID, unlinks only the still-matching losing marker, `fsync`s the parent directory (or the platform durability equivalent), and raises `NameAttachRejectedError`. Failpoints exist after the uniqueness rollback and before unlink, after unlink, and after directory `fsync`. A crash retry performs the same cleanup, removes only the still-matching losing marker, and does not clobber a later winning marker.

No-marker init whose ID-targeted project-plus-`register` transaction fails because this machine already has that root on another project (`CheckoutRootTakenError`), including the race where the other project claims the root after this attempt published its marker: confirm the published UUID has no project row and that another project owns `(machine_id, root_path)`, unlink only this attempt's still-matching marker, `fsync` the parent directory, and raise `CheckoutRootTakenError`. Failpoints exist after the rolled-back transaction, after unlink, and after directory `fsync`. A crash retry performs the same cleanup and does not clobber a later winning marker.

No-marker init that publishes a marker and then raises `OverlayRegistrationRejectedError` — whether `validate_checkout_root` detects an overlay after publication and before the project-plus-`register` transaction begins, or `register`'s same-transaction overlay recheck detects an overlay inserted after that validation — confirms the published UUID has no project row, unlinks only this attempt's still-matching marker, `fsync`s the parent directory, and re-raises `OverlayRegistrationRejectedError`. Keep the settled same-transaction overlay recheck; do not add an advisory-lock subsystem. For the `validate_checkout_root` path, failpoints exist after the typed refusal and before unlink, after unlink, and after directory `fsync`. For the `register` recheck path, failpoints exist after the rolled-back transaction, after unlink, and after directory `fsync`. A crash retry performs the same cleanup and does not clobber a later winning marker.

`initialize_project` and `get_or_create` follow this matrix. “Overlay” means a registered local worktree/clone or an isolation root.

| Case | Result |
| --- | --- |
| Valid marker id, unused name, non-overlay root, no checkout | Create project if needed; `register` this machine |
| Valid marker id, existing active project, no checkout on this machine | `register` |
| Valid marker id, existing soft-deleted project, user-invoked init | Atomic restore-plus-register for this machine |
| Valid marker id, existing soft-deleted project, that name is now an active project on another UUID | `NameAttachRejectedError`; do not restore, register, or rewrite the marker |
| Valid marker id, same machine, same root | Idempotent `register` |
| Valid marker id, same machine, different ordinary root | `CheckoutConflictError`; tell the user to `gobby projects rebind` |
| Valid marker id, other machine, different root | `register` for this machine; one logical project |
| Valid marker at an overlay path | `OverlayRegistrationRejectedError`; do not rebind primary |
| Copied marker at a second non-overlay root on the same machine | `CheckoutConflictError` |
| Missing/malformed/mismatched/ambiguous marker | typed marker error; do not guess |
| No marker, unused name | Write marker UUID, then one transaction for project plus `register` |
| No marker, unused name, this machine's root already owned by another project | Unlink only this attempt's still-matching marker, fsync the directory, raise `CheckoutRootTakenError` |
| No marker, unused name, race: another project claims this machine/root after marker publish | Same cleanup and `CheckoutRootTakenError`; do not leave a marker UUID with no project row |
| No marker, unused name, race: root is an overlay after marker publish (`validate_checkout_root` or `register` recheck) | Unlink only this attempt's still-matching marker, fsync the directory, raise `OverlayRegistrationRejectedError` |
| No marker, same root, two writers, different explicit names | One complete winning marker/project payload; loser adopts that payload or rejects without mutation |
| No marker, two distinct non-overlay roots, same unused name | One DB winner; loser unlinks only its still-matching marker, fsyncs the directory, and raises `NameAttachRejectedError` |
| No marker, name exists (including soft-deleted) | `NameAttachRejectedError` |
| Relative path, unexpanded `~`, or nonexistent path | typed validation error; do not persist |
| Any `CHECKOUT_FREE_PROJECT_IDS` member | `CheckoutSentinelRejectedError`; not a checkout |

Stop `ON CONFLICT (name) DO UPDATE` attach. Stop backfilling `repo_path` from cwd because the name matched.

For an existing marker ID, `projects.name` is authoritative. ID-targeted `ensure_exists`, user-invoked init, and hook ingress must not update `projects.name` from the marker `name`. When that local marker `name` differs, refresh only that still-matching local marker to the database name after the checkout lookup or `register` succeeds, using the shared expected-id helper in the init module. A later init on another machine with a stale marker keeps the database name and refreshes that machine's marker; it does not revert a rename.

That helper is the only later rewrite path. It preserves the existing payload (`id`, `created_at`, and any other fields) and updates only `name`. It refuses an `id` change with `MarkerMismatchError` and does not install. Write complete JSON to a sibling temporary file, `fsync` that file, then exclusive-`flock` the existing marker, re-read its `id`, and install the temporary file only when that `id` still equals `expected_project_id`; a missing file or different `id` refuses without replacing. Then `fsync` the parent directory. Do not `os.replace` over a replacement. Do not add an overlay advisory-lock subsystem; the flock is only the refresh install window. Failpoints exist after temporary-file `fsync`, after install, and after directory `fsync`. A concurrent replacement with a different `id` stays; the refresh refuses. Rename uses this helper as best-effort after the database commit and warns on refusal or write failure; the committed `projects.name` stays authoritative.

**Acceptance:**

- 2.1.1 - The init matrix above holds, including reject-on-existing-name, overlay refusal, and sentinel refusal. test: `tests/utils/test_utils_project_init.py`.
- 2.1.2 - `validate_checkout_root` never expands input; it rejects relative paths, unexpanded `~`, nonexistent paths, overlays, and marker mismatches, and accepts only a platform-local normalized absolute. test: `tests/utils/test_checkout_root.py`.
- 2.1.3 - Marker-first then one project-plus-checkout transaction retries after a marker-only failpoint without `NameAttachRejectedError`. test: `tests/utils/test_utils_project_init.py`.
- 2.1.4 - Two concurrent no-marker initializers at one root produce one winning marker and project; the loser adopts that ID and does not leave a marker pointing at a missing row. test: `tests/utils/test_utils_project_init.py`.
- 2.1.5 - Marker publication writes a complete fsynced temporary file, installs it with no-overwrite, fsyncs the directory, and retries after each publication failpoint without exposing a partial marker. test: `tests/utils/test_utils_project_init.py`.
- 2.1.6 - Two concurrent no-marker initializers at distinct roots with the same unused name leave one project; the loser unlinks only its still-matching marker, fsyncs the directory, and raises `NameAttachRejectedError`. test: `tests/utils/test_utils_project_init.py`.
- 2.1.7 - User-invoked init on a valid marker for a soft-deleted project restores and registers in one transaction; `rebind` still preserves `deleted_at`. test: `tests/utils/test_utils_project_init.py`.
- 2.1.8 - After a losing-name uniqueness rollback, failpoints before unlink, after unlink, and after directory `fsync` cannot resurrect a marker UUID with no project row; retry removes only the still-matching losing marker and preserves any later replacement marker. test: `tests/utils/test_utils_project_init.py`.
- 2.1.9 - Two same-root no-marker writers with different explicit names leave one deterministic marker `id`/`name`/`created_at` and matching project row. test: `tests/utils/test_utils_project_init.py`.
- 2.1.10 - User-invoked init on a valid marker for a soft-deleted project whose name is now active on another UUID raises `NameAttachRejectedError` and rolls back restore, register, and marker rewrite. test: `tests/utils/test_utils_project_init.py`.
- 2.1.11 - No-marker init that publishes a UUID and then loses project-plus-`register` to `CheckoutRootTakenError` unlinks only that still-matching marker, fsyncs the directory, and retries after rollback, unlink, and directory-fsync failpoints without leaving a marker UUID that has no project row. test: `tests/utils/test_utils_project_init.py`.
- 2.1.12 - No-marker init that publishes a UUID and then loses the same-transaction overlay recheck raises `OverlayRegistrationRejectedError`, unlinks only that still-matching marker, fsyncs the directory, and retries after rollback, unlink, and directory-fsync failpoints without leaving a marker UUID that has no project row. test: `tests/utils/test_utils_project_init.py`.
- 2.1.13 - No-marker init that publishes a UUID and then loses `validate_checkout_root` to `OverlayRegistrationRejectedError` unlinks only that still-matching marker, fsyncs the directory, and retries after pre-unlink, post-unlink, and directory-fsync failpoints without leaving a marker UUID that has no project row. test: `tests/utils/test_utils_project_init.py`.
- 2.1.14 - After a committed rename, ID-targeted init or `ensure_exists` on a stale-name marker keeps `projects.name` and refreshes only that local marker; it does not write the stale marker name into the database. test: `tests/utils/test_utils_project_init.py`.
- 2.1.15 - Expected-id marker refresh preserves the payload, refuses an `id` change without overwrite, fsyncs the temporary file, installs only while the on-disk `id` still matches, fsyncs the directory, retries after those failpoints, and leaves a concurrent replacement marker untouched. test: `tests/utils/test_utils_project_init.py`.

### 2.2 Register from hook ingress [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/hooks/project_context.py::ProjectIdResolver.ensure_project_in_db`
- `src/gobby/hooks/project_context.py::resolve_hook_project_context`
- `src/gobby/hooks/hook_manager.py::HookManager._ensure_project_in_db`
- `src/gobby/storage/projects.py::*` — scope-reason: personal ensure stays checkout-free and hook ingress does not register sentinels
- `src/gobby/utils/project_context.py::ensure_project_json_for_isolation`
- `tests/hooks/test_hook_manager.py::*` — scope-reason: cover checkout registration versus overlay cwd

Authenticated local hook ingress with a valid marker on a non-overlay cwd of an active project calls `require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)` first, then `validate_checkout_root` with that returned `machine_id`, then `register`. Overlay cwd resolves the project id and does not register or rebind. A valid marker whose local `name` differs from `projects.name` does not update the database name; refresh only that local marker through the § 2.1 expected-id helper. A valid marker for a soft-deleted project is a typed non-restoring refusal; hook ingress does not clear `deleted_at` and does not register. C1 checkout-domain errors raised from `require_local_machine_id`, `validate_checkout_root`, or `register` — including `SoftDeletedProjectRejectedError`, `OverlayRegistrationRejectedError`, `CheckoutSentinelRejectedError`, `MarkerMismatchError`, `CheckoutRootTakenError`, and `MissingMachineContextError` — propagate out of `ensure_project_in_db`, `ProjectIdResolver.resolve`, and `resolve_hook_project_context`. Those errors inherit `ValueError`; the current `(psycopg.Error, ValueError, RuntimeError)` handler must not catch them. Logging-only remains solely for explicitly non-fatal failures such as `psycopg.Error`. Do not map a typed refusal to `HookProjectResolution(skipped=True)`; that path returns `allow` and continues. A typed refusal must not produce a successful `HookProjectResolution` or apply the marker id as an accepted project. Unknown foreign machine ids are not claimed. Only the cwd-marker path through `resolve` and `ensure_project_in_db` registers or refreshes a marker. Explicit, session, existing-session, contract-probe, and current-context resolutions do not register, rebind, or refresh. Do not add registration logic to `hook_manager` beyond the one-line `_ensure_project_in_db` delegate, and do not wait for terminal admission before that cwd-path register. `HookManager._ensure_project_in_db` stays a one-line delegate.

`ensure_personal_project` upserts the `_personal` project row and must not `register` or write a checkout. File location for personal content is `#20238`. Hook ingress that resolves to any `CHECKOUT_FREE_PROJECT_IDS` member does not register. `ensure_project_json_for_isolation` still copies the marker and still must not create a checkout.

**Acceptance:**

- 2.2.1 - Non-overlay hook ingress registers the local checkout; overlay cwd, isolation copies, and checkout-free sentinel startup do not. test: `tests/hooks/test_hook_manager.py`.
- 2.2.2 - Hook ingress on a valid marker for a soft-deleted project refuses without restoring or registering; user-invoked init remains the restore path. test: `tests/hooks/test_hook_manager.py`.
- 2.2.3 - Each typed checkout-domain refusal from hook registration propagates to the hook ingress boundary; no successful `HookProjectResolution` or session/event continuation follows. test: `tests/hooks/test_hook_manager.py`.
- 2.2.4 - A stale-name hook request leaves `projects.name` unchanged, refreshes only that still-matching local marker through the § 2.1 expected-id helper, and leaves a concurrently replaced marker untouched. test: `tests/hooks/test_hook_manager.py`.
- 2.2.5 - Explicit, session, existing-session, contract-probe, and current-context resolutions do not register, rebind, or refresh a marker; only the cwd-marker `ensure_project_in_db` path does. test: `tests/hooks/test_hook_manager.py`.

### 2.3 Add rebind CLI and stop --repo-path [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/cli/projects.py::update_project`
- `src/gobby/cli/projects.py::list_projects`
- `src/gobby/cli/projects.py::show_project`
- `src/gobby/cli/projects.py::repair_project`
- `src/gobby/cli/projects.py::resolve_refresh_root`
- `src/gobby/cli/projects.py::rename_project`
- `tests/cli/test_projects.py::*` — scope-reason: replace repo_path update/repair coverage with rebind, checkout display, and checkout-free rename

Add `gobby projects rebind PROJECT_REF [PATH]`. Default path is cwd. Call `require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)` first, then `validate_checkout_root` (matching marker, refuse overlay, refuse sentinels, normalize the opaque root) with that returned `machine_id`, then `rebind` atomically. UUID resolution is exact. Name resolution including soft-deleted rows must be unique: an active name wins if present; one matching deleted name is allowed; two or more deleted rows with that name are `AmbiguousProjectRefError` unless PATH’s marker UUID selects exactly one. Do not clear `deleted_at`. This is the cutover abort repair path and uses the § 1.2 rebind branches (absent insert, same-root no-op, different-root update). Remove `--repo-path` from `update`. List/show print the current machine’s checkout as its own field, not as project identity. `repair` no longer writes `projects.repo_path` from cwd. It reports checkout/marker drift and follows this matrix after the same validator:

| Case | Result |
| --- | --- |
| Missing row, valid same-root marker, ordinary root | `register`; report creation |
| Overlay path | typed refuse; no persist |
| Sentinel project | typed refuse; no persist |
| Marker mismatch | typed refuse; no persist |
| Invalid root (relative, unexpanded `~`, nonexistent) | typed refuse; no persist |
| Existing checkout, different root | typed refuse; tell the user to `rebind`; no persist |
| Existing checkout, same root | report no drift; no persist |

Only the missing-row plus valid same-root marker branch persists.

`gobby projects rename PROJECT_REF NEW_NAME` updates `projects.name` and does not read `Project.repo_path`. The database commit is the rename; it succeeds when the calling daemon has no checkout. When a calling-daemon checkout exists, refresh only that local marker through the § 2.1 expected-id helper as best-effort metadata after the database commit and warn on refresh failure, including `MarkerMismatchError`. Do not walk other machines. A later ID-targeted init or hook ingress on a stale marker follows § 2.1: keep `projects.name` and refresh that local marker.

**Acceptance:**

- 2.3.1 - Rebind verifies the marker and updates only this machine’s checkout; `--repo-path` is gone; list/show show the local checkout separately. test: `tests/cli/test_projects.py`.
- 2.3.2 - Rebind resolves a unique soft-deleted project by UUID or name, preserves `deleted_at`, and changes only that machine’s checkout; ambiguous deleted names require UUID or the PATH marker. test: `tests/cli/test_projects.py`.
- 2.3.3 - `repair` registers a missing row only when the same-root marker is valid, reports creation then, refuses overlay/sentinel/marker-mismatch/invalid-root/conflicting-existing-row without persistence, reports no-op for same-root existing rows, and never writes `projects.repo_path`. test: `tests/cli/test_projects.py`.
- 2.3.4 - Rename commits `projects.name` with no local checkout; with a local checkout it refreshes only that marker through the § 2.1 expected-id helper after commit; a post-commit marker-write or `MarkerMismatchError` still leaves the database name changed and warns; it never reads `Project.repo_path`. test: `tests/cli/test_projects.py`.

### 2.4 Expose checkout HTTP and drop repo_path from project JSON [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/projects.py::ProjectUpdate`
- `src/gobby/servers/routes/projects.py::_project_to_response`
- `src/gobby/servers/routes/projects.py::update_project`
- `src/gobby/servers/routes/projects.py::create_projects_router`
- `src/gobby/mcp_proxy/tools/hub.py::*` — scope-reason: stop selecting and returning projects.repo_path
- `tests/servers/routes/test_projects_routes.py::*` — scope-reason: replace repo_path update tests with checkout routes and path-free payloads

Remove `repo_path` from `ProjectUpdate` and `_project_to_response`. `_project_to_response` loads the calling-daemon checkout with `LocalProjectCheckoutManager.get`. Missing checkout, including every present checkout-free sentinel, yields `checkout: null` plus the existing default `approval_rules` and `validation_detection` values; it must not call `require_root` and must not read another machine’s row. Reserve `require_root` for filesystem operations and settings writes. A settings write without a local checkout is 409 `CheckoutNotFoundError`. Add `checkout` for that daemon only.

Add the three checkout routes from C1 with those request, response, and status mappings. `POST` register calls `require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)` first, then `validate_checkout_root` with that returned `machine_id`, then `register`, and takes 201/200 from `register`'s `created` flag in the same transaction. Concurrent same-root register requests produce exactly one 201; remaining successes are 200. Unavailable local machine identity on register or rebind is 409 `MissingMachineContextError`; do not leak `RuntimeError`. `POST` rebind calls `require_local_machine_id(path_machine_id, resource_kind="project_checkout", resource_id=project_id)` first; a foreign path `machine_id` is 409 before filesystem access. Then call `validate_checkout_root` with the returned local `machine_id` and `rebind`. HTTP register on a soft-deleted project is 409 `SoftDeletedProjectRejectedError` and does not restore. HTTP rebind is the preserving operator-repair path: it uses the § 1.2 branches and does not clear `deleted_at`. Do not add settings routes.

**Acceptance:**

- 2.4.1 - Project JSON has no `repo_path`, includes the calling machine checkout or `checkout: null`, serializes checkout-free sentinels without calling `require_root`, and checkout register/rebind reject foreign machine ids before filesystem access and reject overlays and marker mismatches. test: `tests/servers/routes/test_projects_routes.py`.
- 2.4.2 - `GET /api/projects/{project_id}/checkouts` returns only the calling daemon object-or-null, including 200/`checkout: null` for a present sentinel; a second machine’s checkout row is absent from the first machine’s response. test: `tests/servers/routes/test_projects_routes.py`.
- 2.4.3 - Register is 201 then 200 on same-root retry, concurrent same-root requests yield exactly one 201 and remaining successes 200, rebind is 200, unavailable local machine identity on register and rebind is 409 `MissingMachineContextError`, and the C1 typed-error HTTP mapping holds. test: `tests/servers/routes/test_projects_routes.py`.
- 2.4.4 - Local-checkout settings reads succeed, null-checkout list/get use defaults with no filesystem access, settings writes call `require_root`, missing checkout is 409, and another machine’s checkout is never used. test: `tests/servers/routes/test_projects_routes.py`.
- 2.4.5 - HTTP register refuses a soft-deleted project without restoring; HTTP rebind preserves `deleted_at` and neither route clears it. test: `tests/servers/routes/test_projects_routes.py`.

## P3: Daemon filesystem consumers
`kind: framing`

**Goal:** Every ordinary filesystem consumer uses `resolve_operation_root` or `require_root`. Overlay callers keep passing the worktree/clone path. P3 depends on § 1.3, not on CLI/HTTP, and may proceed in parallel with P2.

### 3.1 Cut over build and dispatch roots [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/build/input_resolution.py::*` — scope-reason: replace project.repo_path with require_root
- `src/gobby/build/target_branch.py::*` — scope-reason: resolve the primary checkout before git
- `src/gobby/build/branch_cleanup.py::*` — scope-reason: resolve the primary checkout before branch deletion
- `src/gobby/build/control_artifacts.py::*` — scope-reason: resolve the primary checkout for artifacts
- `src/gobby/build/delivery.py::*` — scope-reason: resolve the primary checkout for GitHub URL detection
- `src/gobby/build/workspaces.py::*` — scope-reason: resolve the primary checkout for integration workspaces
- `src/gobby/cli/build.py::*` — scope-reason: fail closed without a checkout instead of missing repo_path
- `src/gobby/dispatch/spawn.py::*` — scope-reason: spawn from require_root or the explicit overlay
- `src/gobby/dispatch/spawn_artifacts.py::*` — scope-reason: construct git managers from resolved roots
- `src/gobby/dispatch/workspace_merge.py::*` — scope-reason: merge against the primary checkout
- `tests/build/test_input_resolution.py::*` — scope-reason: ordinary checkout, overlay, and missing-checkout cases
- `tests/build/test_target_branch.py::*` — scope-reason: primary checkout before git
- `tests/build/test_clean_branches.py::test_branch_cleanup_refuses_missing_project_repo_path`
- `tests/cli/test_build.py::*` — scope-reason: fail closed without a checkout
- `tests/dispatch/test_dispatcher.py::*` — scope-reason: spawn checkout success, overlay cwd, and missing-checkout failure
- `tests/dispatch/test_workspace_merge.py::*` — scope-reason: merge against the primary checkout and fail closed without one

Replace every `project.repo_path` read in these modules with `require_root` or `resolve_operation_root(..., overlay_path=worktree_or_clone)`. Missing machine or checkout is a typed failure, not a skipped git operation with a guessed cwd. Build tests cover ordinary, overlay, and missing-checkout roots. Dispatch tests cover spawn and workspace-merge checkout success plus each family's relevant overlay and missing-checkout branch.

**Acceptance:**

- 3.1.1 - Build ordinary operations use the machine checkout; explicit worktree/clone paths still win; missing checkout fails closed. test: `tests/build/test_input_resolution.py`.
- 3.1.2 - Branch cleanup refuses a missing checkout instead of a missing `repo_path`. test: `tests/build/test_clean_branches.py::test_branch_cleanup_refuses_missing_project_repo_path`.
- 3.1.3 - Dispatch spawn and workspace-merge use the machine checkout or a registered overlay and fail closed without a checkout. test: `tests/dispatch/test_dispatcher.py`. test: `tests/dispatch/test_workspace_merge.py`.

### 3.2 Cut over agents, sessions, and MCP tool roots [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/app_context.py::*` — scope-reason: construct WorktreeGitManager from require_root
- `src/gobby/agents/lifecycle_monitor.py::*` — scope-reason: resolve the session machine checkout
- `src/gobby/mcp_proxy/tools/task_repo_paths.py::*` — scope-reason: project root comes from the resolver
- `src/gobby/mcp_proxy/tools/tasks/_context.py::RegistryContext.get_project_repo_path`
- `src/gobby/mcp_proxy/tools/tasks/_affected_files.py::*` — scope-reason: pass session.machine_id into the machine-qualified resolver
- `src/gobby/mcp_proxy/tools/tasks/_expansion_registry.py::*` — scope-reason: pass session.machine_id into the machine-qualified resolver
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_paths.py::*` — scope-reason: prefer worktree overlay, else session.machine_id
- `src/gobby/mcp_proxy/tools/tasks/_stage_review.py::*` — scope-reason: pass session.machine_id into the machine-qualified resolver
- `src/gobby/mcp_proxy/tools/task_commits.py::*` — scope-reason: thread the verified session machine into resolve_task_repo_path and resolve_project_repo_path
- `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py::*` — scope-reason: thread the verified session machine into resolve_task_repo_path
- `crates/gcore/assets/schema/baseline.sql::*` — scope-reason: replace resolve_tool_session with the checkout-only four-column definition on fresh/test schema
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: regenerate baseline checksum after the resolve_tool_session replacement
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: synchronize Python expected identity after the intermediate baseline mutation
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pin resolve_tool_session return columns on fresh schema
- `src/gobby/mcp_proxy/tools/tasks/_delivery.py::*` — scope-reason: delivery push uses require_root or worktree
- `src/gobby/mcp_proxy/tools/sessions/_commits.py::*` — scope-reason: commit listing uses the resolved root
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::*` — scope-reason: spawn factory uses resolver or overlay
- `src/gobby/mcp_proxy/tools/communications.py::*` — scope-reason: stop emitting repo_path as project_path identity
- `src/gobby/servers/middleware/project_context.py::*` — scope-reason: request context carries checkout, not repo_path
- `src/gobby/servers/websocket/chat/_session.py::*` — scope-reason: session.project_path is the resolved checkout
- `src/gobby/storage/managed_credentials.py::*` — scope-reason: consume resolve_tool_session machine_id and root_path
- `tests/mcp_proxy/tools/test_task_repo_paths.py::*` — scope-reason: cover missing checkout and overlay preference
- `tests/mcp_proxy/tools/test_task_commits.py::*` — scope-reason: preserve descendant and overlay authorization after the machine-qualified resolver
- `tests/mcp_proxy/tools/tasks/test_close_task_flow.py::*` — scope-reason: close-task git cwd uses the machine-qualified resolver
- `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py::*` — scope-reason: replace repo_path-only project mocks with session-machine checkout or resolver fakes
- `tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py::*` — scope-reason: replace repo_path-only project mocks with session-machine checkout or resolver fakes
- `tests/mcp_proxy/tools/test_task_worktree_lifecycle_decoupling.py::*` — scope-reason: replace repo_path-only project mocks with session-machine checkout or resolver fakes
- `tests/agents/test_lifecycle_monitor.py::*` — scope-reason: session-machine checkout success and missing-checkout failure
- `tests/servers/test_project_context_middleware.py::*` — scope-reason: request context carries checkout, not repo_path
- `tests/servers/websocket/chat/test_session.py::*` — scope-reason: session.project_path is the resolved checkout
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: spawn factory checkout, overlay, and missing-checkout branches
- `tests/mcp_proxy/test_mcp_tools_session_messages.py::*` — scope-reason: session-message project_path uses checkout, not repo_path
- `tests/storage/test_managed_credentials.py::*` — scope-reason: accept a registered local overlay path and reject unregistered, wrong-project, and foreign-machine paths
- `tests/storage/test_postgres_agent_authorization.py::*` — scope-reason: reject mismatched session.machine_id vs issuing_machine_id

`RegistryContext.get_project_repo_path` must take `machine_id` and call the resolver. Callers in `_affected_files.py`, `_expansion_registry.py`, `_lifecycle_paths.py`, and `_stage_review.py` pass `session.machine_id` when the tool has a session, otherwise `require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)`. `_lifecycle_paths.py` still prefers `artifacts.worktree_path` when that overlay is registered.

`task_commits.py` and `tasks/_lifecycle_close.py` pass the verified resolved-session `machine_id` into `resolve_task_repo_path` / `resolve_project_repo_path`. Keep explicit descendant and task/ancestor overlay validation. Invalid explicit paths stay typed failures. Do not fall back to the daemon machine when the session machine is missing.

This deliverable owns the final `gobby_agent_auth.resolve_tool_session` four-column checkout-only definition on fresh and test schema: `RETURNS (session_id, project_id, machine_id, root_path)` using a `LEFT JOIN` from each eligible session to `project_checkouts` on `sessions.machine_id` + `sessions.project_id` only, keeping `COALESCE(session.status, 'active') NOT IN ('expired', 'deleted')`, and permitting a null `root_path` when no primary checkout exists. Expired or deleted sessions still return no row. Do not leave the live three-column `projects.repo_path` function in baseline. § 6.1 applies that same definition transactionally to populated live databases after checkout coverage is proven.

`resolve_tool_session` Python consumers read `root_path` and `machine_id`, never `projects.repo_path`. Require `session.machine_id == issuing_machine_id`. The consumer must verify the returned machine is the local daemon. Mismatched, missing, or foreign machine identities fail closed.

`ManagedCredentialManager.issue_tool_request` authorizes `requested_project_path` after those session project and machine checks through overlay-aware resolution. Classify the requested path: if it equals a non-null checkout `root_path`, call `resolve_operation_root(..., overlay_path=None)`; if it is a registered local worktree or clone for that `project_id` and machine, call `resolve_operation_root(..., overlay_path=requested_project_path)` even when `root_path` is null; every other non-null path is a typed refusal. Do not pass a primary checkout string as `overlay_path`. Do not require a primary checkout before accepting a valid registered overlay.

Replace `Project.repo_path` mocks in `test_task_lifecycle_coverage.py`, `test_tasks_lifecycle_coverage.py`, and `test_task_worktree_lifecycle_decoupling.py` with session-machine checkout or resolver fakes. Lifecycle-monitor, project-context middleware, websocket session, spawn-factory, and session-message tests exercise checkout success plus each family's relevant overlay or missing-checkout branch.

**Acceptance:**

- 3.2.1 - Task-path MCP ordinary filesystem work fails closed without `(project_id, machine_id)` checkout context, prefers a registered overlay when provided, and refuses a foreign session machine before filesystem access. test: `tests/mcp_proxy/tools/test_task_repo_paths.py`.
- 3.2.2 - A session/grant pair with mismatched, missing, or foreign machine identities is rejected. test: `tests/storage/test_postgres_agent_authorization.py`.
- 3.2.3 - Fresh/test `resolve_tool_session` returns `(session_id, project_id, machine_id, root_path)` from a `LEFT JOIN` to `project_checkouts` only, permits a null `root_path` when no primary checkout exists, and the listed task-tool callers compile against the machine-qualified signature. file: `crates/gcore/assets/schema/baseline.sql`.
- 3.2.4 - `issue_tool_request` accepts a registered local overlay, including when the session has no primary checkout, and rejects unregistered, wrong-project, and foreign-machine requested paths. test: `tests/storage/test_managed_credentials.py`.
- 3.2.5 - Fresh/test `resolve_tool_session` returns no row for expired or deleted sessions. test: `tests/storage/test_postgres_agent_authorization.py`.
- 3.2.6 - After installing the four-column `resolve_tool_session` definition, regenerated `assets.rs` identities and `schema_expected_identity.json` match that intermediate baseline. file: `crates/gcore/src/schema/assets.rs`. file: `src/gobby/storage/schema_expected_identity.json`.
- 3.2.7 - Lifecycle-monitor, project-context middleware, websocket session, spawn-factory, and session-message families resolve the session-machine checkout and fail closed on the family's relevant missing or foreign context. test: `tests/agents/test_lifecycle_monitor.py`. test: `tests/servers/test_project_context_middleware.py`. test: `tests/servers/websocket/chat/test_session.py`. test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py`. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py`.
- 3.2.8 - Task-lifecycle coverage tests stop mocking `Project.repo_path` and use session-machine checkout or resolver fakes. test: `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py`. test: `tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py`. test: `tests/mcp_proxy/tools/test_task_worktree_lifecycle_decoupling.py`.
- 3.2.9 - An eligible overlay-only session returns a `resolve_tool_session` row with null `root_path`; `issue_tool_request` then authorizes a registered local overlay and refuses missing, invalid, and foreign overlays; expired or deleted sessions still return no row. test: `tests/storage/test_managed_credentials.py`. test: `tests/storage/test_postgres_agent_authorization.py`.

### 3.3 Cut over files, source control, plans, and workflows [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/files.py::*` — scope-reason: every path is rooted at require_root
- `src/gobby/servers/routes/source_control.py::*` — scope-reason: source-control git cwd is require_root
- `src/gobby/servers/routes/skills.py::*` — scope-reason: skill root is require_root
- `src/gobby/servers/session_changes.py::*` — scope-reason: change listing uses require_root
- `src/gobby/cli/plans.py::*` — scope-reason: plan files resolve through require_root
- `src/gobby/plans/handoff_manifest_service.py::*` — scope-reason: handoff root is require_root
- `src/gobby/plans/review_evidence.py::*` — scope-reason: review evidence root is require_root
- `src/gobby/plans/review_manifest_service.py::*` — scope-reason: review manifest root is require_root
- `src/gobby/storage/plans.py::*` — scope-reason: stored plan paths resolve through require_root
- `src/gobby/tasks/expansion/_compile.py::*` — scope-reason: expansion project root is require_root
- `src/gobby/workflows/hooks.py::*` — scope-reason: dirty-file project_path is overlay-aware resolve_operation_root
- `src/gobby/workflows/imports.py::*` — scope-reason: workflow import roots use require_root
- `src/gobby/wiki/scope_resolution.py::*` — scope-reason: wiki project root is require_root
- `src/gobby/cli/linear.py::*` — scope-reason: linear project.json updates use require_root
- `src/gobby/memory/dream/service.py::*` — scope-reason: dream project path is require_root
- `src/gobby/scheduler/executor.py::*` — scope-reason: cron project_path is require_root
- `src/gobby/utils/project_context.py::_build_and_set_project_context`
- `src/gobby/utils/project_context.py::get_workflow_project_path`
- `src/gobby/utils/project_context.py::set_project_context_from_session`
- `src/gobby/cli/sessions.py::*` — scope-reason: session cwd comes from require_root
- `src/gobby/servers/routes/sessions/core.py::*` — scope-reason: stop selecting projects.repo_path
- `tests/servers/routes/test_sessions_routes.py::TestGetCommitCount`
- `src/gobby/plans/bootstrap_ledger.py::*` — scope-reason: ledger repo_root is require_root
- `src/gobby/hooks/event_handlers/_session_start/agents.py::*` — scope-reason: wiki overview uses the resolver
- `src/gobby/mcp_proxy/tools/plans/__init__.py::*` — scope-reason: plan project_root is require_root
- `src/gobby/servers/routes/mcp/hook_hold_open.py::*` — scope-reason: hook project_path is require_root
- `src/gobby/sync/linear_project_ops.py::*` — scope-reason: linear path reads use require_root
- `src/gobby/workflows/engine/templating.py::*` — scope-reason: template project path is require_root
- `src/gobby/servers/routes/admin/_testing.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/servers/routes/test_files.py::*` — scope-reason: ordinary, overlay, and missing-checkout file roots
- `tests/servers/routes/test_source_control_routes.py::*` — scope-reason: missing checkout is 409
- `tests/plans/test_bootstrap_ledger_revalidation.py::*` — scope-reason: ledger uses require_root
- `tests/cli/test_sessions.py::*` — scope-reason: session cwd fails closed without a checkout
- `tests/utils/test_project_context.py::*` — scope-reason: project_context stops reading Project.repo_path
- `tests/servers/routes/test_skills_routes.py::*` — scope-reason: skill roots use checkout, missing checkout, and sentinel cases
- `tests/cli/test_linear_coverage.py::*` — scope-reason: Linear CLI project.json updates use checkout, missing checkout, and sentinel cases
- `tests/sync/test_linear_sync.py::*` — scope-reason: Linear sync path reads use checkout, missing checkout, and sentinel cases
- `tests/cli/test_plans.py::*` — scope-reason: plan CLI roots use checkout and fail closed without one
- `tests/plans/test_handoff_manifest_service.py::*` — scope-reason: handoff root uses checkout
- `tests/plans/test_review_evidence.py::*` — scope-reason: review evidence root uses checkout
- `tests/wiki/test_scope_resolution.py::*` — scope-reason: wiki root uses checkout and skips sentinels
- `tests/scheduler/test_cron_executor.py::*` — scope-reason: cron project_path uses checkout
- `tests/memory/test_dream.py::*` — scope-reason: dream project path uses checkout
- `tests/workflows/test_hooks.py::*` — scope-reason: workflow dirty-file uses registered overlay, primary checkout, and typed overlay refusal
- `tests/servers/test_session_changes.py::*` — scope-reason: session-change listing uses checkout

Do not add lines to `source_control.py` beyond the root lookup. Missing checkout is 409/typed error, not a silent empty diff. `src/gobby/wiki/scope_resolution.py` uses `require_root` for ordinary projects and must not call it for any `CHECKOUT_FREE_PROJECT_IDS` member. Admin `_testing` inserts a checkout row, not `projects.repo_path`. `list_projects` stops emitting `repo_path` and returns the same path-free calling-daemon `checkout` object-or-null project JSON as `_project_to_response`; it does not invent a second serializer. `_get_commit_count` stops selecting `projects.repo_path`. It resolves the git cwd from `session.machine_id` plus the checkout resolver. Local checkout success uses that root; missing checkout, missing machine, and foreign-machine refusal return 0 without running git. Replace `repo_path`-row fakes in `TestGetCommitCount` with session-machine plus checkout-resolver fakes. Each leftover family — plans, wiki, scheduler, dream, workflows, and session-changes — exercises a local checkout success and its relevant missing-checkout or sentinel branch in the named test.

Workflow dirty-file path resolution is overlay-aware. `WorkflowHookHandler._resolve_project_path` stops reading `Project.repo_path`. It keeps preferring `event.cwd` then `metadata.project_path` and still runs that candidate through the existing git worktree-root helper. When `event.project_id` is present, resolve the local machine with `require_local_machine_id(None, resource_kind="project_checkout", resource_id=project_id)` and classify the worktree root: a registered local worktree or clone is `resolve_operation_root(..., overlay_path=that_root)` and is inspected even when no primary checkout exists; no candidate, or a candidate that equals this machine's primary checkout, uses `require_root` / `overlay_path=None`; every other non-null candidate (unregistered, wrong-project, foreign-machine) is a typed refusal. When `event.project_id` is absent, keep the cwd/metadata git-root result and do not invent a checkout. Do not inspect dirty files against the primary checkout when a registered overlay is present.

**Acceptance:**

- 3.3.1 - Files routes resolve the calling machine checkout and fail closed when it is missing. test: `tests/servers/routes/test_files.py`.
- 3.3.2 - Source-control missing checkout is 409, not an empty diff. test: `tests/servers/routes/test_source_control_routes.py`.
- 3.3.3 - `project_context` and session cwd no longer read `Project.repo_path`. test: `tests/utils/test_project_context.py`.
- 3.3.4 - Skills routes, Linear CLI, and Linear sync resolve a local checkout, fail closed without one, and skip `require_root` for checkout-free sentinels. test: `tests/servers/routes/test_skills_routes.py`. test: `tests/cli/test_linear_coverage.py`. test: `tests/sync/test_linear_sync.py`.
- 3.3.5 - `GET /api/files/projects` returns checkout-shaped project JSON (`checkout` object or `checkout: null`) and never `repo_path`. test: `tests/servers/routes/test_files.py`.
- 3.3.6 - Plans, wiki, scheduler, dream, workflows, and session-changes resolve a local checkout and fail closed or skip sentinels on each family's relevant missing-checkout branch. test: `tests/cli/test_plans.py`. test: `tests/plans/test_handoff_manifest_service.py`. test: `tests/plans/test_review_evidence.py`. test: `tests/wiki/test_scope_resolution.py`. test: `tests/scheduler/test_cron_executor.py`. test: `tests/memory/test_dream.py`. test: `tests/workflows/test_hooks.py`. test: `tests/servers/test_session_changes.py`.
- 3.3.7 - Session commit-count uses the session machine checkout resolver and covers local success, missing checkout, missing machine, and foreign-machine refusal before git runs. test: `tests/servers/routes/test_sessions_routes.py::TestGetCommitCount`.
- 3.3.8 - Workflow dirty-file checks inspect a registered local overlay even when no primary checkout exists, use the primary checkout when the candidate is absent or equals that checkout, and raise a typed refusal for invalid or foreign overlays. test: `tests/workflows/test_hooks.py`.

### 3.4 Cut over isolation reconciliation and runner startup [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `src/gobby/runner_maintenance/isolation_reconciliation.py::_reconcile_isolation_registry`
- `src/gobby/runner_maintenance/isolation_reconciliation.py::_reconcile_project_worktrees`
- `src/gobby/runner_maintenance/isolation_reconciliation.py::_reconcile_project_clones`
- `src/gobby/runner_maintenance/isolation.py::*` — scope-reason: isolation maintenance uses require_root
- `src/gobby/runner_lifecycle_subsystems.py::*` — scope-reason: wiki-job cleanup keys off missing checkout, not missing repo_path
- `src/gobby/storage/tasks/_live_session_recovery.py::*` — scope-reason: recovery uses the session machine checkout
- `tests/test_isolation_reconciliation.py::*` — scope-reason: isolation reconciliation enumerates this machine’s checkouts
- `tests/test_runner_project_recovery.py::*` — scope-reason: runner wiki-job cleanup keys off missing local checkout
- `tests/storage/tasks/test_live_session_recovery.py::*` — scope-reason: filter by local machine before filesystem I/O

Scan `project_checkouts` for this machine instead of “projects with a repo_path”. Adopt worktrees/clones relative to that checkout. Startup that today removes `gobby:wiki-*` jobs when `repo_path` is missing or not a directory does the same when this machine has no checkout or the checkout directory is gone. Do not treat overlay paths as missing primaries.

Filter recovery candidates by the local machine before any filesystem operation. A foreign session whose opaque checkout string matches a local path is not inspected. Checkout-free sentinel IDs are not recovery checkouts.

**Acceptance:**

- 3.4.1 - Isolation reconciliation and runner startup enumerate this machine’s checkouts and leave overlay registries intact. test: `tests/test_isolation_reconciliation.py`.
- 3.4.2 - Recovery ignores a foreign session whose path string matches a local directory. test: `tests/storage/tasks/test_live_session_recovery.py`.
- 3.4.3 - Runner startup removes `gobby:wiki-*` jobs when this machine has no checkout or the checkout directory is gone, and does not treat overlay paths as missing primaries. test: `tests/test_runner_project_recovery.py`.

## P4: Gcode grants and index rebind
`kind: framing`

**Goal:** Gcode resolves a project through the local checkout. Rebind drops only this machine’s active index state.

### 4.1 Resolve gcode projects through project_checkouts [category: code] (depends: P1)
`kind: deliverable`

Targets:
- `crates/gcode/src/config/context.rs::resolve_project_by_name`
- `crates/gcode/src/config/context.rs::resolve_project_identity`
- `crates/gcode/src/config/context.rs::resolve_project_id`
- `crates/gcode/src/config/tests.rs::*` — scope-reason: cover checkout-based name and id resolution

`resolve_project_by_name` and id/identity resolution join `project_checkouts` with `read_local_machine_id()`. Do not use `code_indexed_project_states.root_path` as the primary project locator; that column stays overlay/view metadata. If cwd is a registered overlay, keep current overlay behavior. Name lookup that only exists as an index view on another machine is a miss on this machine.

Gcode name lookup is active-only: join `projects` with `projects.deleted_at IS NULL`. The single active checkout wins when same-name soft-deleted rows exist. A deleted-only name is a miss. Do not adopt the § 2.3 CLI rebind deleted-name policy here. Exact marker or UUID resolution remains the explicit path for non-name identity, including soft-deleted projects.

4.1 consumes the grant installed by 1.1 (`projects (id, name, deleted_at)` plus `project_checkouts` SELECT and lock-only UPDATE `(machine_id, project_id, root_path)` scoped to the grant machine). Active-name resolution consumes that `deleted_at` column grant. Do not retarget the privilege manifest here. `#18902` wraps that same 1.1 grant. Legacy `projects.repo_path` grant removal stays exclusively in 6.1. Primary indexing uses that UPDATE privilege only for `SELECT ... FOR SHARE`; it does not add a SECURITY DEFINER helper.

**Acceptance:**

- 4.1.1 - Gcode name and id resolution use the local checkout, not `projects.repo_path` or another machine’s index root. test: `crates/gcode/src/config/tests.rs`.
- 4.1.2 - Gcode name lookup matches only `projects.deleted_at IS NULL`; a deleted-only name is a miss; the single active checkout wins when deleted duplicates exist; UUID or marker resolution remains the explicit non-name path. test: `crates/gcode/src/config/tests.rs`.

### 4.2 Invalidate only the rebound machine index view [category: code] (depends: 2.3)
`kind: deliverable`

Targets:
- `src/gobby/storage/project_checkouts.py`
- `crates/gcode/src/index/indexer/lifecycle.rs::invalidate`
- `crates/gcode/src/index/indexer/lifecycle.rs::refresh_project_stats`
- `crates/gcode/src/index/api.rs::upsert_project_seed`
- `crates/gcode/src/index/api.rs::upsert_project_stats`
- `crates/gcode/src/index/api.rs::adopt_file_state`
- `crates/gcode/src/index/api.rs::delete_file_state`
- `crates/gcode/src/index/indexer/file.rs::index_file`
- `crates/gcode/src/index/indexer/file.rs::index_content_only`
- `crates/gcode/src/index/indexer/pipeline.rs::index_discovered_files`
- `crates/gcode/src/index/indexer/pipeline.rs::index_explicit_files_with_connection`
- `crates/gcode/src/index/indexer/overlay.rs::*` — scope-reason: overlay upserts stay checkout-independent view writes
- `crates/gcode/src/index/indexer/sink.rs::*` — scope-reason: overlay sink upserts stay checkout-independent view writes
- `crates/gcode/src/commands/search_regression_tests.rs::*` — scope-reason: choose explicit upsert mode at this direct caller
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: choose explicit upsert mode at this direct caller
- `src/gobby/code_index/gcode_gateway.py::GcodeGateway.invalidate_project_by_id`
- `src/gobby/code_index/_storage/projects.py::CodeIndexProjectStorageMixin.upsert_project_stats`
- `src/gobby/code_index/_storage/files.py::CodeIndexFileStorageMixin.upsert_file`
- `src/gobby/code_index/_storage/files.py::CodeIndexFileStorageMixin.delete_file`
- `tests/storage/test_project_checkouts.py`
- `tests/code_index/test_storage.py::*` — scope-reason: Python primary upsert refuses a stale root; overlay-view writes remain
- `tests/code_index/test_sync_worker.py::*` — scope-reason: choose explicit upsert mode at this direct caller
- `tests/mcp_proxy/test_plans_tools.py::*` — scope-reason: choose explicit upsert mode at this direct caller
- `crates/gcode/src/index/api_tests.rs::*` — scope-reason: primary versus overlay mode at the upsert boundary
- `crates/gcode/src/index/indexer/tests/api_contract.rs::*` — scope-reason: primary upsert after rebind must not recreate the old root
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: both direct adopt_file_state cases choose explicit mode and seed a matching checkout for primary

Serialize rebind per project using the § 1.2 branches. In one database transaction: absent row inserts the validated root and preserves active index state only when none exists or its recorded root matches the inserted checkout; when existing primary state records another root, the same transaction inserts the checkout and clears that machine/project’s active project and file states. Cover pre-cutover leftover state and abort-rebind-rerun. Same root returns unchanged with no timestamp mutation and no invalidation; different root `SELECT ... FOR UPDATE`s the checkout row, updates it, and clears `code_indexed_project_states` plus per-file active states for `(machine_id, project_id)` only. Concurrent absent-row `rebind` uses the § 1.2 INSERT-then-on-conflict `FOR UPDATE` protocol so the loser enters the same-root or different-root branch instead of surfacing a raw primary-key violation. Shared parsed content, vectors, graph data, and other machines stay. Do not issue a post-commit database invalidation; the transaction already cleared old active state. Any external-projection cleanup must be qualified by the old root or generation and must not delete current-root database state. Every primary active-state write — `upsert_project_seed`, `upsert_project_stats` (including `refresh_project_stats`), `adopt_file_state`, `delete_file_state`, the per-file transactions in `index_file` / `index_content_only`, and Python `CodeIndexFileStorageMixin.upsert_file` / `delete_file` — `SELECT ... FOR SHARE` the matching checkout row and holds that lock through that write’s existing transaction, then requires committed-root equality. `upsert_file` and `delete_file` take an explicit primary-versus-overlay mode with authoritative root input at every caller already named here (`tests/code_index/test_storage.py`, `tests/code_index/test_sync_worker.py`, `tests/mcp_proxy/test_plans_tools.py`). Primary mode locks the matching checkout and requires root equality; overlay mode does not lock a checkout. Add paused primary Python file upsert/delete versus different-root rebind tests. Keep discovery and hashing before the first database write. Do not wrap the whole pipeline in one transaction. Overlay mode does not lock a checkout. Do not add an advisory-lock subsystem or a SECURITY DEFINER lock helper; `gobby_gcode_capability` uses the § 1.1 lock-only UPDATE grant. `upsert_project_seed` / `upsert_project_stats` take an explicit primary-versus-overlay mode at every caller, including `lifecycle.rs::refresh_project_stats`, `search_regression_tests.rs`, `serial_db.rs`, `test_sync_worker.py`, and `test_plans_tools.py`. Direct `adopt_file_state` callers include `crates/gcode/src/commands/status/content_gc/tests.rs`; both adoption cases choose a mode explicitly and seed a matching checkout for every primary case. Primary callers seed a matching checkout and require committed-root equality. Overlay callers opt into overlay mode and stay checkout-independent. Destination-side discovery and hashing still run before adopting shared content (`invalidate_postgres_deletes_only_machine_state` is the existing contract). After a different-root rebind, `code_indexed_project_states.root_path` for a later primary index must equal the new checkout.

**Acceptance:**

- 4.2.1 - Different-root rebind removes only the selected machine’s active index states and leaves shared content and other-machine state in place; same-root rebind and absent-row insert with no state or a matching recorded root do not delete index state; absent-row insert with a different recorded root clears that machine/project’s active project and file states. test: `tests/storage/test_project_checkouts.py`.
- 4.2.2 - Crash mid-rebind, a concurrent stale-root primary index writer, and a paused post-commit callback cannot expose the new checkout with old active state or delete current-root state. test: `tests/storage/test_project_checkouts.py`.
- 4.2.3 - Full and explicit primary pipelines refuse a root that is not the committed checkout; overlay upserts still write view rows. test: `crates/gcode/src/index/indexer/tests/api_contract.rs`.
- 4.2.4 - The Python project-state upsert refuses a stale primary root and still writes overlay-view rows without a checkout. test: `tests/code_index/test_storage.py`.
- 4.2.5 - The upsert API exposes primary versus overlay mode; every listed direct caller, including `refresh_project_stats` and both `crates/gcode/src/commands/status/content_gc/tests.rs` `adopt_file_state` cases, chooses a mode explicitly. test: `crates/gcode/src/index/api_tests.rs`. test: `crates/gcode/src/commands/status/content_gc/tests.rs`.
- 4.2.6 - A paused primary seed or stats writer holding `FOR SHARE` cannot write old-root state after a different-root rebind takes `FOR UPDATE`; overlay mode still writes without a checkout lock. test: `crates/gcode/src/index/indexer/tests/api_contract.rs`. test: `tests/code_index/test_storage.py`.
- 4.2.7 - Destination-side discovery and hashing run before adopting shared content; the adjacent full-index case reparses previously adopted content. test: `crates/gcode/src/index/indexer/tests/serial_db.rs::indexing_adopts_existing_content_version_without_reparse`. test: `crates/gcode/src/index/indexer/tests/serial_db.rs::full_indexing_reparses_previously_adopted_content`.
- 4.2.8 - A paused primary file-state upsert, adopt, delete, or orphan-cleanup writer holding `FOR SHARE` cannot recreate old-root active file state after a different-root rebind; overlay file writes remain checkout-independent. test: `crates/gcode/src/index/indexer/tests/api_contract.rs`.
- 4.2.9 - The Python file-state upsert and delete refuse a stale primary root, hold `FOR SHARE` through the write, and still write overlay-view rows without a checkout. test: `tests/code_index/test_storage.py`.

## P5: Web checkout field
`kind: framing`

**Goal:** The web app uses the calling daemon checkout, not `repo_path`.

### 5.1 Replace web repo_path with checkout [category: code] (depends: 2.4, 3.3)
`kind: deliverable`

Targets:
- `web/src/hooks/useProjects.ts::ProjectWithStats`
- `web/src/hooks/useProjects.ts::ProjectUpdateFields`
- `web/src/hooks/useProjects.ts::useProjects`
- `web/src/hooks/useFiles.ts::*` — scope-reason: file roots use checkout.root_path
- `web/src/hooks/__tests__/useFiles.test.ts::*` — scope-reason: files project list accepts checkout object and checkout: null
- `web/src/components/chat/BranchIndicator.tsx::BranchIndicator`
- `web/src/hooks/__tests__/useProjects.test.tsx::*` — scope-reason: search and display use checkout
- `web/src/components/chat/__tests__/BranchIndicator.test.tsx::*` — scope-reason: main repo path comes from checkout
- `web/src/components/activity/fields/__tests__/DateTimeField.test.tsx::*` — scope-reason: ProjectWithStats fixture uses checkout
- `web/src/components/activity/mcp/__tests__/McpServerFields.test.tsx::*` — scope-reason: ProjectWithStats fixture uses checkout
- `web/src/components/app/__tests__/useAppProjectSelection.test.tsx::*` — scope-reason: ProjectWithStats fixture uses checkout
- `web/src/__tests__/App.test.tsx::*` — scope-reason: replace logical project repo_path fixtures with checkout objects or checkout: null
- `web/src/components/activity/skills/__tests__/SkillsTab.test.tsx::*` — scope-reason: replace ProjectWithStats repo_path fixture with checkout
- `web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx::*` — scope-reason: replace HTTP project-response repo_path with checkout or checkout: null
- `web/tests/activity-panel-changes-session-scope.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/activity-panel-web-chat-sessions.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/epic-10452-verification.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/file-editor.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/provider-picker.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/style-surfaces.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/terminal-colors.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/web-chat-restore-plan.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout
- `web/tests/web-chat-swap-send-respond.spec.ts::*` — scope-reason: replace logical project repo_path fixtures with checkout

`ProjectWithStats` drops `repo_path` and adds `checkout: { machine_id: string; root_path: string } | null`. Search uses `checkout.root_path` when present and omits path text when `checkout` is null. `BranchIndicator` sets the main repo from `checkout.root_path` and renders a no-checkout state without throwing. `ProjectUpdateFields` cannot send `repo_path`. `useFiles` consumes checkout-shaped `/api/files/projects` JSON with both object and null checkout cases. Every typed `ProjectWithStats` constructor and logical HTTP project fixture uses the checkout shape, including an explicit null-checkout case, the App, SkillsTab, and WikiA11y fixtures, and the named Playwright project-response fixtures. Preserve unrelated worktree `repo_path` fields. Check desktop and mobile project list, project switcher, and branch indicator.

Verify this leaf with focused Vitest for the named web tests, focused Playwright for the named spec files, plus `npm --prefix web run type-check` and `npm --prefix web run lint`.

**Acceptance:**

- 5.1.1 - Project list, search, files, and BranchIndicator use `checkout.root_path` and never read `repo_path`. test: `web/src/hooks/__tests__/useProjects.test.tsx`. test: `web/src/hooks/__tests__/useFiles.test.ts`.
- 5.1.2 - Remaining `ProjectWithStats` and logical HTTP project fixtures, including App, SkillsTab, WikiA11y, `useFiles`, and the named Playwright specs, construct `checkout` (including null) and never `repo_path`. test: `web/src/components/activity/fields/__tests__/DateTimeField.test.tsx`. test: `web/src/__tests__/App.test.tsx`. test: `web/src/components/activity/skills/__tests__/SkillsTab.test.tsx`. test: `web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx`. test: `web/src/hooks/__tests__/useFiles.test.ts`. test: `web/tests/file-editor.spec.ts`.
- 5.1.3 - Null-checkout project list, search, and BranchIndicator render without throwing and omit path identity. test: `web/src/hooks/__tests__/useProjects.test.tsx`.

### 5.2 Pin two-machine checkout identity [category: test] (depends: 2.4, 4.2, 5.1)
`kind: deliverable`

Targets:
- `tests/integration/test_project_checkout_identity.py`

Create one focused isolated-daemon integration module. Do not talk to the daily daemon. One marker and one project across two machine contexts and two ordinary roots. Each HTTP caller receives only its checkout. A foreign-machine register/rebind is 409. Overlay cwd keeps overlay precedence and never writes a checkout. Rebind on machine A leaves machine B’s checkout row and active index state intact.

**Acceptance:**

- 5.2.1 - Two-machine HTTP, overlay refusal, and one-machine rebind compose without leaking the other machine’s checkout or index state. test: `tests/integration/test_project_checkout_identity.py`.

## P6: Fenced project-checkout-cutover
`kind: framing`

**Goal:** One maintenance campaign inserts the proven checkout set, drops `projects.repo_path`, and writes the target receipt.

### 6.1 Run the project-checkout-cutover campaign [category: code] (depends: 2.2, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 5.1)
`kind: deliverable`

Targets:
- `src/gobby/storage/project_checkout_cutover.py`
- `src/gobby/storage/maintenance_epoch.py::*` — scope-reason: add project-checkout-cutover to Campaign and admission
- `src/gobby/cli/hub_maintenance.py::*` — scope-reason: wire rehearsal, live, resume, and post-verify messaging
- `crates/gcore/assets/schema/baseline.sql`
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: regenerate catalog without projects.repo_path
- `crates/gcore/assets/schema/seed.manifest.json::*` — scope-reason: drop repo_path from sentinel project seeds
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: regenerate identity after dropping repo_path
- `crates/gcore/src/schema/verify.rs::*` — scope-reason: drop projects.repo_path from the column allowlist
- `crates/gcore/src/schema/runner.rs::*` — scope-reason: recognize today's 375 receipt as the project-checkout predecessor
- `crates/gcore/src/schema/runner_tests.rs::*` — scope-reason: insert checkout rows instead of projects.repo_path and pin predecessor classification
- `crates/gcore/tests/schema_contract.rs::*` — scope-reason: pin the target schema without projects.repo_path
- `src/gobby/storage/schema_expected_identity.json::*` — scope-reason: synchronize Python expected identity
- `crates/gcode/security/managed_postgres_privileges.json::*` — scope-reason: remove projects.repo_path from grants
- `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`
- `tests/storage/test_schema_contract.py::test_production_python_has_no_persistent_postgres_ddl`
- `tests/storage/test_project_checkout_cutover.py`
- `src/gobby/storage/account_identity_cutover.py::*` — scope-reason: preserve project-checkout-cutover in _TARGET_CAMPAIGNS and known-constraint recognition
- `tests/storage/test_account_identity_cutover.py::*` — scope-reason: add project-checkout-cutover to CAMPAIGNS and the baseline CHECK-constraint set
- `tests/e2e/conftest.py::*` — scope-reason: stop inserting projects.repo_path; create verified machine/checkout rows
- `tests/integration/test_hub_query.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/cli/test_import.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/mcp_proxy/test_metrics_manager.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/mcp_proxy/test_metrics_store.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/mcp_proxy/test_registries.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/mcp_proxy/tools/test_apply_persona.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/mcp_proxy/tools/test_hub.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/mcp_proxy/tools/workflows/test_import.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/memory/test_manager.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/plans/test_plan_coverage_ci.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/sessions/test_e2e_session_tracking.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/sessions/test_token_usage.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/storage/test_checkpoints.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/storage/test_manager_surface_parity.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/storage/test_task_affected_files.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/workflows/test_pipeline_heartbeat.py::*` — scope-reason: stop inserting projects.repo_path
- `tests/storage/test_project_manager.py::*` — scope-reason: rewrite Project.repo_path identity assertions
- `tests/storage/test_project_repo_path_isolation.py::*` — scope-reason: rewrite isolation assertions around checkout-free sentinels and overlay-preserves-primary
- `crates/gcore/tests/catalog_manifest_freshness.rs::*` — scope-reason: retarget runtime mutation to surviving schema after the column drop
- `tests/cli/test_hub_maintenance.py::*` — scope-reason: campaign lazy-load, rehearsal, live, resume, refusal, and operator messages
- `tests/storage/test_postgres_agent_authorization.py::*` — scope-reason: after the column drop, pin checkout-only resolve_tool_session and the gone repo_path column
- `tests/sync/test_github_issue_sync.py::*` — scope-reason: remove obsolete Project.repo_path assignment and create(..., repo_path=) leftovers
- `tests/integration/test_edit_history.py::*` — scope-reason: migrate positional LocalProjectManager.create ordinary-root setup to verified machine, marker, project, and checkout
- `tests/e2e/test_worktrees_e2e.py::*` — scope-reason: migrate git_repo_with_origin marker fixture off repo_path and register a verified machine, project, and checkout

Reuse the account-identity epoch/backup/evidence/resume/verify framework. Add campaign `project-checkout-cutover`.

Predecessor bootstrap, before any candidate insert and without refreshing the baseline receipt: execute `CREATE TABLE IF NOT EXISTS project_checkouts`, then verify the complete table, constraint, policy, and grant shape (PK, unique root, FKs, forced RLS, daemon-runtime access, migration-owner access, the 1.1 project-and-machine capability SELECT policy, and the lock-only UPDATE grant) on databases that already recorded baseline 375. Do not refresh the receipt. This is campaign DDL, not automatic predecessor-receipt refresh. Update the production-Python DDL inventory in `tests/storage/test_schema_contract.py` with the exact post-implementation operation counts for `src/gobby/storage/project_checkout_cutover.py`, including that `CREATE TABLE IF NOT EXISTS`. Keep exact equality so unexpected `CREATE`, `ALTER`, or `DROP` still fails.

Preflight, for every non-empty legacy `projects.repo_path` on a non-sentinel project:

- Candidate machines come from machine-owned evidence: sessions, `code_indexed_project_states`, worktrees, clones.
- Coverage is per `(machine_id, project_id)` pair, not per project. One or more verified existing checkout rows are authoritative: preserve every such row and that machine’s index state; do not insert, update, or delete those rows. A project that already has valid rows on two machines and no unresolved pair is covered: zero inserts, both rows and both machines’ index state preserved.
- A non-sentinel legacy-path project whose authoritative checkout set and candidate-machine set are both empty is a `no_candidate_machine` preflight abort. Retain `projects.repo_path`, record that status in rehearsal evidence, and direct the operator to `gobby projects rebind` on the owning daemon. Do not treat an empty candidate-machine set as covered.
- Only candidate `(machine_id, project_id)` pairs that lack a checkout row are unresolved. Each unresolved pair requires exactly one locally validated candidate. Zero or multiple unresolved candidates abort only for pairs with no authoritative row.
- Each unresolved candidate calls `require_local_machine_id(candidate_machine_id, resource_kind="project_checkout", resource_id=project_id)` before filesystem access. Pass the returned id to `validate_checkout_root` (exists, normalized absolute, marker match, not an overlay, not a sentinel). Persist only through `LocalProjectCheckoutManager.register` inside the campaign's ambient transaction. `register.created` must match the recorded insert set exactly. Foreign-machine inferred rows abort before filesystem access; the operator runs `gobby projects rebind` on the owning daemon, including for soft-deleted projects.
- `_global`, `_migrated`, `_orphaned`, and `_personal` stay checkout-free even if a path is present.
- Soft-deleted ordinary projects with a path follow the same two branches. `rebind` must resolve them without clearing `deleted_at`.
- Record the expected insert set. Do not hardcode a machine’s row count in this plan; put live counts in rehearsal evidence.

Rehearsal and abort evidence emit, per legacy project: project id and name, legacy root, candidate machine ids, evidence sources, any existing checkout rows, exclusion reason, and resolution status. Direct the operator to run `gobby projects rebind` on the owning daemon for unresolved rows. Do not invent a mapping file or a second migration subsystem.

In one transaction: lock the relevant tables, revalidate preflight, persist exactly the unresolved locally validated checkout rows through `LocalProjectCheckoutManager.register` (skip already-covered `(machine_id, project_id)` pairs; preserve every existing authoritative row; `register.created` equals the recorded insert set), verify coverage, drop `projects.repo_path`, replace live `resolve_tool_session` by `DROP FUNCTION gobby_agent_auth.resolve_tool_session(UUID)` without `CASCADE` then `CREATE FUNCTION` with the § 3.2 four-column `LEFT JOIN` definition including nullable `root_path` and `COALESCE(session.status, 'active') NOT IN ('expired', 'deleted')`, restore `SECURITY DEFINER`, `search_path`, ownership, `PUBLIC` revoke, and runtime `EXECUTE`, update grants so `gobby_gcode_capability` keeps `SELECT (id, name, deleted_at)` on `projects` and loses only `repo_path`, update `crates/gcode/security/managed_postgres_privileges.json` plus `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set` so final `projects` columns are `id`, `name`, and `deleted_at` only, write the target baseline receipt last. Rollback on any mismatch, including function recreation failure. `CREATE OR REPLACE` is not used because it cannot change `RETURNS TABLE` from three columns to four.

Staged gcore must classify the live pre-epic 375 receipt as `ProjectCheckoutPredecessor` and the post-cutover receipt as `AlreadyBaselined`. Capture today's live `baseline@375` checksum as `PROJECT_CHECKOUT_PREDECESSOR_CHECKSUM` when 1.1/6.1 retarget `BASELINE_CHECKSUM`. The operator instruction is `gobby hub-maintenance run project-checkout-cutover`. Do not treat that live receipt as `CorruptPartial`.

After success: regenerate catalog, seed, expected identity, and Rust/Python contract fixtures. Rehearse against the epoch backup, run live with matching staged binaries, verify schema identity and checkout coverage, then release the fence. Do not call `_start_daemon()` after target-schema application. Follow the existing `account-identity-cutover` handoff: print the operator instruction to install the staged `gdaemon` and `gcode` binaries, then start the daemon and health-check it manually. Predecessor-only abort (schema still 375 with `projects.repo_path`) may start the installed pre-epic daemon. Applied-target abort must not start that pre-epic binary; the operator resumes or recovers with the staged binaries.

Post-cutover residue contract `tests/storage/test_project_checkout_cutover.py::test_identity_repo_path_residue_allowlist` fails if any of these remain in production sources, contracts, or tests: `Project.repo_path` as identity, a read or write of `projects.repo_path`, a managed grant that lists that column, logical project JSON that carries `repo_path`, or a positional ordinary-root argument to `LocalProjectManager.create` / `ensure_exists` / `update` that still treats a filesystem path as identity without a verified machine, marker, and checkout. Allowed leftovers are campaign input before the transaction, campaign test fixtures that feed that input, and ordinary function parameters or Git locals that merely hold a resolved filesystem path. Unowned test fixtures named above migrate here: stop inserting the column, create verified machine/checkout rows where a filesystem root is required, rewrite personal/isolation assertions around checkout-free sentinels and overlay-preserves-primary, remove the obsolete `Project.repo_path` assignment in `tests/sync/test_github_issue_sync.py`, migrate `tests/integration/test_edit_history.py` positional `create(name, root)` setup to a verified machine, marker, project, and checkout, and migrate `tests/e2e/test_worktrees_e2e.py` `git_repo_with_origin` to the stable marker schema without `repo_path` plus verified machine, project, and checkout registration. The test names the exact gcode query set it runs.

After the column drop, the authorization fixture must not be able to `SELECT projects.repo_path` (the column is gone), must still `SELECT projects.deleted_at` and evaluate `deleted_at IS NULL` as `gobby_gcode_capability`, and `resolve_tool_session` must return `(session_id, project_id, machine_id, root_path)` from the § 3.2 `LEFT JOIN` definition, including a null `root_path` for an eligible overlay-only session.

**Acceptance:**

- 6.1.1 - Successful populated migration, covered-row rerun with zero inserts, two valid existing machine checkout rows perform zero inserts and preserve both rows and both machines’ index state, zero/multiple unresolved rejection only without an authoritative checkout, `no_candidate_machine` abort for a non-sentinel legacy-path project with empty checkout and candidate-machine sets then abort-then-rebind-then-rerun success, abort-then-rebind-then-rerun success for local and foreign machines, transactional rollback, prompt-free resume, soft-deleted abort/rebind/rerun, sentinel exclusion, local-validation refusal, and receipt verification all pass. test: `tests/storage/test_project_checkout_cutover.py`.
- 6.1.2 - Target baseline has no `projects.repo_path`, live `resolve_tool_session` matches the § 3.2 checkout-only definition, gcode grants no longer select `repo_path` while still selecting `deleted_at`, and privilege-manifest parity lists `projects` columns `id`, `name`, and `deleted_at` only. file: `crates/gcore/assets/schema/baseline.sql`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.
- 6.1.3 - Post-cutover residue allowlist fails on identity-bearing `repo_path` leftovers in production and tests, including positional ordinary-root `create` / `ensure_exists` / `update` arguments, and allows only campaign input plus ordinary path locals. test: `tests/storage/test_project_checkout_cutover.py::test_identity_repo_path_residue_allowlist`.
- 6.1.4 - Campaign bootstrap executes `CREATE TABLE IF NOT EXISTS project_checkouts` on an already-receipted 375 database, then verifies the complete table, constraint, policy, and grant shape, including the 1.1 daemon-runtime, migration-owner, and project-and-machine capability SELECT policies plus the lock-only UPDATE grant, and does not refresh the receipt. test: `tests/storage/test_project_checkout_cutover.py`.
- 6.1.5 - Hub-maintenance lazy-load, rehearsal, live, resume, refusal, and operator messages work for `project-checkout-cutover`. test: `tests/cli/test_hub_maintenance.py`.
- 6.1.6 - `verify.rs` and `runner_tests.rs` no longer encode `projects.repo_path`. file: `crates/gcore/src/schema/verify.rs`.
- 6.1.7 - Campaign registry and baseline CHECK constraints include `project-checkout-cutover`; `account_identity_cutover.py` `_TARGET_CAMPAIGNS` and known-constraint recognition preserve that value, and constraint replacement retains the expanded set. test: `tests/storage/test_account_identity_cutover.py`.
- 6.1.8 - Live `resolve_tool_session` is dropped without `CASCADE` and recreated with the § 3.2 four-column `LEFT JOIN` shape, including nullable `root_path`; recreation failure rolls back. test: `tests/storage/test_project_checkout_cutover.py`.
- 6.1.9 - Staged gcore classifies today's live 375 receipt as the project-checkout predecessor and the target receipt as already-baselined. test: `crates/gcore/src/schema/runner_tests.rs`.
- 6.1.10 - After the column drop, regenerated `catalog.manifest.json`, `seed.manifest.json`, `assets.rs` identities, `schema_expected_identity.json`, catalog freshness, and schema-contract tests match the `repo_path`-free schema. file: `crates/gcore/assets/schema/catalog.manifest.json`. file: `crates/gcore/assets/schema/seed.manifest.json`. test: `crates/gcore/tests/catalog_manifest_freshness.rs`. test: `crates/gcore/tests/schema_contract.rs`.
- 6.1.11 - Live and fresh `resolve_tool_session` still return no row for expired or deleted sessions after drop-then-create. test: `tests/storage/test_postgres_agent_authorization.py`.
- 6.1.12 - After target-schema application, hub-maintenance does not auto-start the installed pre-epic daemon; predecessor-only abort may start it; applied-target abort leaves the fence for staged-binary resume. test: `tests/cli/test_hub_maintenance.py`.
- 6.1.13 - Unresolved candidates call `require_local_machine_id` before filesystem access, persist only through `LocalProjectCheckoutManager.register` in the campaign transaction, and `register.created` matches the recorded insert set. test: `tests/storage/test_project_checkout_cutover.py`.
- 6.1.14 - The production-Python DDL inventory includes exact post-implementation operation counts for `src/gobby/storage/project_checkout_cutover.py` and still fails on unexpected DDL. test: `tests/storage/test_schema_contract.py::test_production_python_has_no_persistent_postgres_ddl`.

## T1: Verification
`kind: verification`

- Two machines, one marker, two roots, one project. Named artifact: `tests/integration/test_project_checkout_identity.py`.
- Same-machine same-root idempotent; second ordinary root conflicts; second project at that root is taken.
- Overlay cwd never creates a checkout.
- Unix and Windows root strings survive hub storage unchanged.
- Checkout-free sentinels never receive a checkout row.
- Two concurrent no-marker inits converge on one marker and project.
- HTTP mutations reject machine mismatches and unavailable local machine identity with 409 `MissingMachineContextError`. Logical project JSON has no path; `checkout` is the caller’s row. GET `/checkouts` is object-or-null; register is 201/200; rebind is 200.
- Rename commits `projects.name` without a local checkout and refreshes only the calling-daemon marker when a checkout exists, through the expected-id helper that refuses a replacement marker. A later init or hook ingress on a stale-name marker keeps the database name.
- Different-root rebind invalidates only that machine’s active index state. Same-root rebind and absent-row rebind with no state or matching-root state do not invalidate; absent-row rebind with mismatched recorded-root state clears only that machine/project’s active project and file states. Primary upserts cannot recreate the old root.
- Soft-deleted rebind preserves `deleted_at` and unblocks cutover rerun. HTTP register refuses soft-deleted projects. Ambiguous deleted names require UUID. Gcode name lookup is active-only (`deleted_at IS NULL`); deleted-only names miss. The capability grant includes `projects.deleted_at` so that predicate is legal.
- One or more verified existing checkout rows, including two valid machine rows for one project, rerun with zero inserts and preserve those rows and both machines’ index state; unresolved-candidate abort applies only to `(machine_id, project_id)` pairs with no authoritative row.
- Gcode adopts shared content only after destination hashing.
- Two issued capabilities see only their own checkout row (`tests/storage/test_postgres_agent_authorization.py`).
- Campaign tests listed in 6.1.1, including abort evidence, abort-then-rebind-then-rerun, local-validation refusal, and `_personal` exclusion.
- Hub-maintenance campaign path: `tests/cli/test_hub_maintenance.py`.
- Post-cutover residue allowlist: `tests/storage/test_project_checkout_cutover.py::test_identity_repo_path_residue_allowlist`.
- Recovery ignores a foreign session whose path string matches a local directory.
- Focused protected pytest, scoped Ruff/Mypy, gcode tests, gcore schema-contract tests. No full pytest.
- Frontend leaf 5.1: focused Vitest for `web/src/hooks/__tests__/useProjects.test.tsx`, `web/src/components/chat/__tests__/BranchIndicator.test.tsx`, the named `ProjectWithStats` fixture tests, and `web/src/__tests__/App.test.tsx`, `web/src/components/activity/skills/__tests__/SkillsTab.test.tsx`, and `web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx`, plus `npm --prefix web run type-check` and `npm --prefix web run lint`.
- Live `resolve_tool_session` is drop-then-create, not `CREATE OR REPLACE`. Staged gcore treats today's 375 receipt as the project-checkout predecessor.

## V1 Plan Changelog
`kind: verification`

**Round 1** `kind: enhancement`

- enhancer_run: fe180cdc-5b24-43b2-b11a-68963605ec55
- enhancer_session: 6b6bceb6-bc3f-433d-b2e9-e32501c255ab
- converged: false
- suggestions_presented: 6
- accepted:
  - E1 / better / post-cutover residue allowlist in T1 and 6.1.3
  - E2 / better / abort evidence plus rebind recovery in 6.1
  - E3 / better / P3 depends on 1.3 and may run in parallel with P2
  - E4 / better / shared `validate_checkout_root` ingress in 2.1–2.4
  - E5 / better / two-machine grant isolation in 1.1.3; column-gone and checkout-only join stay in 6.1.2
  - E6 / better / isolated two-machine integration test as 5.2
- declined:
  - none
- resolution_notes: Folded all six Better suggestions. Split E5 so P1 pins live checkout-row isolation and P6 pins the dropped column plus `resolve_tool_session` join. Added category-test deliverable 5.2 as the E6 owner. P4.2, P5, and P6 gates are unchanged.

**Round 1** `kind: verification`

- reviewer_run: 90cbb3dd-3e91-4dc1-bcb9-ea8aa7767991
- reviewer_session: 81af829e-b760-4e18-830e-0fecbf989b3f
- verdict: needs_review
- findings:
  - PCID-R1-F01 blocking missing-requirement: #19651 promotion unowned
  - PCID-R1-F02 blocking bad-sequencing: 6.1 omitted 2.2 and 5.2
  - PCID-R1-F03 blocking weak-testability: P3 lacked named tests
  - PCID-R1-F04 blocking unhandled-edge: checkout path input policy
  - PCID-R1-F05 blocking traceability: leftover repo_path consumers
  - PCID-R1-F06 blocking traceability: gcore verify.rs and runner_tests.rs
  - PCID-R1-F07 blocking weak-testability: machine-aware test fixtures
  - PCID-R1-F08 blocking traceability: ProjectWithStats fixtures
  - PCID-R1-F09 blocking weak-testability: hub-maintenance campaign tests
  - PCID-R1-F10 blocking bad-sequencing: predecessor bootstrap
  - PCID-R1-F11 blocking unhandled-edge: init crash recovery
  - PCID-R1-F12 blocking unhandled-edge: overlay/checkout register race
  - PCID-R1-F13 blocking unhandled-edge: rebind index atomicity
  - PCID-R1-F14 blocking unhandled-edge: credential/session machine binding
  - PCID-R1-F15 blocking unhandled-edge: cutover local root attestation
  - PCID-R1-F16 blocking unhandled-edge: foreign session recovery filter
- accepted: F01 (Constraints prerequisite), F02 (2.2 hooks only, not 5.2, `_personal` not registered), F03, F04 (wording only), F05, F06, F07 (helper + conftest), F08, F09, F10 (campaign bootstrap), F11 (marker-first + one txn), F13, F14, F15 (local-only insert; `_personal` excluded), F16
- declined: F12 (advisory lock and concurrent race suite; overlay recheck stays in the same transaction)
- resolution_notes: Folded the recommended scoped repairs. `_personal` is checkout-free; hub file location is #20238. 6.1 now depends on 2.2, not 5.2. Campaign bootstrap does not refresh the baseline receipt.

```json plan-review-round
{"evidence_id":"dae2271c-e9ec-4461-ae19-b7c6bfeaef1f","plan_hash":"b3a2b71b797caa2e75fb0b811c5ccde7cd84dff6299a34a1f674d1c241f17a62","round_number":1,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"26f2b61b7d3d3797e742cb446be3b9a77abce20a57d43b505ae5f478c295c85a","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":16,"total":18},"evidence_id":"dae2271c-e9ec-4461-ae19-b7c6bfeaef1f","lanes":[{"candidate_count":6,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"8625d072de37458afdd637366b78b9f1c2fc36e1b0f6b3a8121293ae06708337","status":"valid"},"source_digest":"c02edf6e4c3f1071848989c64a268298c1f686ffca79ece87a5a748123a9793a","version":1},"findings":[{"category":"missing-requirement","check_key":"task-lifecycle-traceability","description":"The plan says #19651 must become the owning epic and the chain must be preserved, but the live task remains a feature.","finding_id":"PCID-R1-F01","fix":"Add a hard preparation prerequisite.","location":"Overview / § 1.1","prevention":"Trace each governing task type, parent, dependency edge, and validation contract to one acceptance item before review.","principle":"Every governing task mutation and dependency edge needs an owned deliverable and acceptance evidence.","root_cause":"The overview requires promoting #19651 and preserving the downstream chain, while no deliverable owns or verifies those task changes.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"cutover-required-gates","description":"Cutover can run before hook registration and integration proof.","finding_id":"PCID-R1-F02","fix":"Add 2.2 and 5.2 to 6.1 dependencies.","location":"P6 / § 6.1","prevention":"Enumerate all schema producers, consumers, and integration-test deliverables.","principle":"Destructive schema removal must depend on every producer, consumer, and integration proof required after cutover.","root_cause":"The cutover dependency set omits deliverables 2.2 and 5.2.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","check_key":"filesystem-consumer-test-evidence","description":"P3 acceptance does not identify focused test files.","finding_id":"PCID-R1-F03","fix":"Add concrete test targets.","location":"P3 / §§ 3.1, 3.3, and 3.4","prevention":"Name the test module, case, and command.","principle":"A broad filesystem-consumer migration needs named focused tests.","root_cause":"P3 acceptance relies on behavior labels.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"checkout-path-input-policy","description":"Ambiguous whether tilde is expanded or rejected.","finding_id":"PCID-R1-F04","fix":"Define one canonical input policy.","location":"P2 / § 2.1","prevention":"Specify accepted path forms and exact errors.","principle":"Persisted identity paths require one exact normalization and rejection contract.","root_cause":"The plan uses both unexpanded tilde examples and user-expanded-path language.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"repo-path-consumer-inventory","description":"Unowned production reads remain.","finding_id":"PCID-R1-F05","fix":"Add leftover consumers to P3 or P6.","location":"P3 / § 3.3","prevention":"Reconcile every production hit.","principle":"A column-removal plan must inventory every production read.","root_cause":"The target inventory omits production consumers.","section_id":"3.3","severity":"blocking"},{"category":"traceability","check_key":"gcore-column-contract-inventory","description":"verify.rs and runner_tests.rs still encode repo_path.","finding_id":"PCID-R1-F06","fix":"Add both gcore files to 6.1 targets.","location":"P6 / § 6.1","prevention":"Sweep allowlists and runner fixtures.","principle":"Schema cutover scope includes verification allowlists and runner fixtures.","root_cause":"The plan omits gcore contracts that still recognize the retired column.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","check_key":"machine-aware-test-fixtures","description":"Factories do not establish machine, marker, or checkout state.","finding_id":"PCID-R1-F07","fix":"Add an isolated-machine helper.","location":"P1 / § 1.3","prevention":"Update the central fixture.","principle":"Machine-owned persistence requires deterministic isolated fixtures.","root_cause":"Shared fixtures supply only repo_path.","section_id":"1.3","severity":"blocking"},{"category":"traceability","check_key":"project-with-stats-fixture-inventory","description":"Web fixtures still supply repo_path.","finding_id":"PCID-R1-F08","fix":"Add every ProjectWithStats fixture to 5.1.","location":"P5 / § 5.1","prevention":"Cover populated and null variants.","principle":"A response-model shape change must include every typed constructor.","root_cause":"Typed ProjectWithStats fixtures still construct repo_path.","section_id":"5.1","severity":"blocking"},{"category":"weak-testability","check_key":"cutover-cli-campaign-tests","description":"Campaign CLI tests are missing.","finding_id":"PCID-R1-F09","fix":"Add hub-maintenance campaign tests.","location":"P6 / § 6.1","prevention":"List lazy-loader, rehearsal, live, resume, refusal, and messaging cases.","principle":"A resumable destructive campaign needs command-path tests.","root_cause":"The plan omits tests/cli/test_hub_maintenance.py.","section_id":"6.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"cutover-predecessor-bootstrap","description":"Cutover can reach insert with no project_checkouts table.","finding_id":"PCID-R1-F10","fix":"Add campaign predecessor bootstrap.","location":"P6 / § 6.1","prevention":"Require an idempotent bootstrap before inserts.","principle":"A resumable cutover must establish predecessor schema on receipted databases.","root_cause":"Editing baseline 375 does not create the table on live databases.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"init-cross-store-crash-recovery","description":"Crash after marker write can strand retry.","finding_id":"PCID-R1-F11","fix":"Write marker first; one ID-targeted transaction.","location":"P2 / § 2.1","prevention":"Enumerate failpoints and prove retry.","principle":"Cross-store initialization must be retry-safe.","root_cause":"Marker and database creation lack a durable recovery order.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"overlay-checkout-registration-race","description":"Checkout and overlay register can race.","finding_id":"PCID-R1-F12","fix":"Advisory lock and concurrent tests.","location":"P2 / § 2.1","prevention":"Identify the transaction lock and add a two-writer race test.","principle":"Mutually exclusive checkout and overlay identities must be enforced atomically.","root_cause":"Validate-then-insert has no shared serialization boundary.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"rebind-index-atomicity","description":"Interrupted rebind can expose stale index state.","finding_id":"PCID-R1-F13","fix":"One transaction plus stale-writer guard.","location":"P4 / § 4.2","prevention":"Document lock, transaction, post-commit side effects, and crash tests.","principle":"A root rebind must define one ordered commit boundary.","root_cause":"psycopg rebind and async gcode invalidation lack ordering.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"credential-session-machine-binding","description":"Mismatched session and grant machines can leak a path.","finding_id":"PCID-R1-F14","fix":"Require equality and test mismatch.","location":"P3 / § 3.2","prevention":"Assert machine identities match.","principle":"A credential scoped to one machine must not resolve another machine path.","root_cause":"session.machine_id and issuing_machine_id are not checked for equality.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"cutover-local-root-attestation","description":"Campaign can insert an unvalidated foreign path.","finding_id":"PCID-R1-F15","fix":"Insert only locally validated candidates.","location":"P6 / § 6.1","prevention":"Separate inference from local attestation.","principle":"A central cutover may persist a root only after local validation.","root_cause":"Inferred candidates are not locally validated.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"foreign-session-recovery-machine-filter","description":"Foreign session path strings can be treated as local.","finding_id":"PCID-R1-F16","fix":"Filter recovery by local machine.","location":"P3 / § 3.4","prevention":"Filter ownership before I/O.","principle":"Filesystem recovery may inspect only local-machine paths.","root_cause":"Hub-wide recovery is not filtered by machine before I/O.","section_id":"3.4","severity":"blocking"}],"reviewer_session":"81af829e-b760-4e18-830e-0fecbf989b3f","round":1,"round_number":1,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

**Round 2** `kind: verification`

- reviewer_run: 19a86be7-f8b6-4ce5-afc1-7ccfac56cb45
- reviewer_session: 9731d596-8931-424a-9151-f31ee553a925
- verdict: needs_review
- findings:
  - PCID-R2-F01 blocking traceability: #18902/#17678 handoff contracts unowned
  - PCID-R2-F02 blocking missing-requirement: checkout-free sentinels only enforced for _personal
  - PCID-R2-F03 blocking missing-requirement: opaque Unix/Windows root acceptance
  - PCID-R2-F04 blocking traceability: four get_project_repo_path callers unowned
  - PCID-R2-F05 blocking unhandled-edge: rebind stale-writer write sites
  - PCID-R2-F06 blocking traceability: privilege-manifest parity test
  - PCID-R2-F07 blocking traceability: campaign registry parity test
  - PCID-R2-F08 blocking traceability: repo_path test-fixture residue
  - PCID-R2-F09 blocking unhandled-edge: concurrent no-marker init
  - PCID-R2-F10 blocking bad-sequencing: resolve_tool_session shape vs 3.2/6.1
  - PCID-R2-F11 blocking unhandled-edge: soft-deleted rebind recovery
  - PCID-R2-F12 blocking unhandled-edge: covered-row cutover preflight abort
- accepted: F01 (Constraints preparation only), F02 (four-sentinel set), F03 (1.2.2 + stale #19651 command in prep), F04 (four callers + session machine), F05 (primary write sites only; overlays stay view writes), F06, F07, F08 (residue sweep + named leftover fixtures, not a 17-leaf inventory), F09 (create-if-absent + loser adopts; no advisory lock), F10 (3.2 owns fresh function; 6.1 applies live), F11, F12 (covered vs one-new branches)
- declined: none as whole findings; narrowed F05/F08/F09 over-scope (overlay-requires-checkout, 17-file deliverable, new lock subsystem)
- resolution_notes: Folded the scoped repairs. Did not relitigate R1 F12. Wiki-home, USER.md, Tailnet, hosted tenancy, and dual-read stay out of this plan.

```json plan-review-round
{"evidence_id":"c7c684b5-a66f-4eca-bbdf-f0bb83bdba20","plan_hash":"56306cc7011a24c1990a417d1dbe5afbad063f2ecc08164549272fd88bf8b3e2","round_number":2,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f3d4475090b2ec1dde3209ee9ec6f2fe065633f2cb612abec52cab2c2193ce53","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":6,"emitted_findings":12,"total":18},"evidence_id":"c7c684b5-a66f-4eca-bbdf-f0bb83bdba20","lanes":[{"candidate_count":5,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":7,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"3430840f4b75dd582d28dfac110dbe23112296e0dd80feeca9e7d4344b1ea8f5","status":"valid"},"source_digest":"2979d3095a7d8d9fe3fef59704f1a19fd3909def1a24629994cc79ed2b9bd9b1","version":1},"findings":[{"category":"traceability","check_key":"cross-plan-handoff-ownership","description":"The handoffs to #18902 and #17678 exist only in this plan; their live task contracts do not yet consume the grant/SQL shape or content-versus-index-view identity obligation.","finding_id":"PCID-R2-F01","fix":"Extend the preparation prerequisite to add the exact C1 grant and resolve_tool_session obligation to #18902 and D1.1 to #17678, with validation criteria and `deferred-from:project-checkout-identity:*` labels, then verify those task fields before expansion.","location":"C1, D1, § 4.1, and § 6.1","prevention":"Before expansion, compare every cross-plan handoff against the destination task description, labels, and validation criteria.","principle":"A deferred or downstream contract is durable only when the destination task explicitly owns its inherited obligation and validation evidence.","root_cause":"The plan assigns C1 to #18902 and D1.1 to #17678, while the live destination tasks have neither project-checkout-identity criteria nor the corresponding deferral labels.","section_id":"C1","severity":"blocking"},{"category":"missing-requirement","check_key":"checkout-free-sentinel-enforcement","description":"Ordinary manager, CLI, hook, and HTTP paths can still create checkout rows for `_global`, `_orphaned`, or `_migrated`; only the cutover excludes all four sentinels.","finding_id":"PCID-R2-F02","fix":"Define one checkout-free sentinel-ID set containing all four seeded identities; require `register`, `rebind`, `require_root`, and every ingress to reject every member with a typed error, and add manager plus ingress/HTTP acceptance cases.","location":"C1 and §§ 1.2–2.4","prevention":"Trace each sentinel identity through register, rebind, require_root, automatic ingress, CLI, HTTP, and campaign paths.","principle":"Every identity declared checkout-free must be rejected by every persistence and resolution entry point.","root_cause":"Constraints exclude `_global`, `_orphaned`, `_migrated`, and `_personal`, while C1 and manager acceptance require typed refusal only for `PERSONAL_PROJECT_ID`.","section_id":"1.2","severity":"blocking"},{"category":"missing-requirement","check_key":"cross-platform-opaque-root-acceptance","description":"The plan has no manifest-covered acceptance item for cross-platform opaque path storage and server-side non-interpretation.","finding_id":"PCID-R2-F03","fix":"Add an acceptance item to § 1.2 or § 5.2 that stores and retrieves Unix and Windows-style root strings unchanged without server reinterpretation, and replace #19651's stale validation command with the planned focused checkout tests during preparation.","location":"§§ 1.2, 1.3, and 5.2","prevention":"Map every governing validation criterion to an existing or explicitly created test artifact and a covered acceptance item.","principle":"A governing cross-platform identity requirement needs executable leaf acceptance, not only verification prose.","root_cause":"#19651 requires one project across drive letters and platforms, but no deliverable acceptance item proves opaque Unix and Windows-style roots, and its current validation command names an absent test file.","section_id":"1.2","severity":"blocking"},{"category":"traceability","check_key":"machine-context-caller-inventory","description":"Four task-tool callers would retain the old call shape or lack a verified machine source after `get_project_repo_path` becomes machine-qualified.","finding_id":"PCID-R2-F04","fix":"Add the four caller modules to § 3.2 and specify whether each passes the session machine ID or `require_local_machine_id()`, with focused caller tests.","location":"§ 3.2","prevention":"Resolve the exact symbol, enumerate all call sites, and assign every caller to a target before approving a signature change.","principle":"A required signature change must own every production caller and its source of the new argument.","root_cause":"`RegistryContext.get_project_repo_path` must take `machine_id`, but `_affected_files.py`, `_expansion_registry.py`, `_lifecycle_paths.py`, and `_stage_review.py` are absent from § 3.2 targets.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"rebind-stale-writer-write-sites","description":"The accepted rebind atomicity repair does not reach the actual Rust and Python writers, so an in-flight old-root index can recreate active project or file state after the transaction clears it.","finding_id":"PCID-R2-F05","fix":"Add `crates/gcode/src/index/api.rs`, explicit/full pipeline entry points, and `src/gobby/code_index/_storage/projects.py` to § 4.2; make every active-state write conditional on the committed `(machine_id, project_id, root_path)` checkout and test both Rust routes plus the Python upsert against concurrent rebind.","location":"§ 4.2","prevention":"For every invalidation race, inventory delete sites and every corresponding create, adopt, and upsert path.","principle":"A stale-writer exclusion must guard every state mutation that can recreate invalidated state.","root_cause":"§ 4.2 owns invalidation, while Rust index API/pipeline upserts and the Python project-state upsert can still write an old root after rebind.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"managed-privilege-registry-parity","description":"The privilege-manifest parity test will fail and does not yet assert checkout columns or machine scoping.","finding_id":"PCID-R2-F06","fix":"Add `tests/code_index/test_gcode_privilege_manifest.py` to § 1.1 and extend its exact relation-set, column, and `machine_id` scope assertions for `project_checkouts`.","location":"§§ 1.1 and 4.1","prevention":"Search registry names through production consumers, exact-set tests, generated manifests, and schema grants whenever adding a relation.","principle":"A registry addition must update every exact-set parity assertion that enumerates registry members.","root_cause":"`project_checkouts` is added to `managed_postgres_privileges.json`, while `tests/code_index/test_gcode_privilege_manifest.py` still hard-codes the current relation set and is unowned.","section_id":"1.1","severity":"blocking"},{"category":"traceability","check_key":"maintenance-campaign-registry-parity","description":"`tests/storage/test_account_identity_cutover.py` is absent from § 6.1 even though its CAMPAIGNS and CHECK-constraint assertions must change.","finding_id":"PCID-R2-F07","fix":"Add that test file to § 6.1 and update its exact Python campaign registry and baseline database CHECK-constraint parity for `project-checkout-cutover`.","location":"§ 6.1","prevention":"Sweep enum literals through registries, database constraints, exhaustive matches, lazy loaders, and exact-set tests.","principle":"A new exhaustive enum or registry member must update its database and test parity inventories.","root_cause":"§ 6.1 adds `project-checkout-cutover`, while the account-identity cutover parity test hard-codes the complete campaign set and database CHECK constraint.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"repo-path-test-fixture-inventory","description":"The target schema would leave broad test surfaces failing, including `tests/e2e/conftest.py`, `tests/integration/test_hub_query.py`, `tests/storage/test_project_manager.py`, `tests/storage/test_project_repo_path_isolation.py`, and `crates/gcore/tests/catalog_manifest_freshness.rs`.","finding_id":"PCID-R2-F08","fix":"Add the complete cited fixture inventory to the owning sections; remove logical-project `repo_path` inserts, create verified machine/checkout rows where filesystem roots are required, rewrite personal/isolation assertions around checkout-free sentinels and overlay-preserves-primary behavior, and retarget the gcore runtime-mutation test to surviving schema.","location":"§§ 2.1, 2.2, and 6.1","prevention":"Run an exact residue sweep across production, contracts, generated sources, and tests; classify every hit before approving a destructive drop.","principle":"Dropping a schema column and model field requires migrating executable test fixtures and assertions as well as production consumers.","root_cause":"The residue inventory is production-focused; seventeen Python SQL fixtures still insert `projects.repo_path`, gcore catalog freshness mutates it, and personal/isolation tests assert `Project.repo_path` semantics.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"init-concurrent-marker-creation","description":"Two initializers can transact different project IDs; the losing name insert may leave the final marker pointing at an ID with no project row.","finding_id":"PCID-R2-F09","fix":"Specify exclusive initial marker creation or a root-scoped lock; the losing initializer must reread and adopt the winning marker ID before its ID-targeted database transaction, with a concurrent no-marker initialization test.","location":"§ 2.1","prevention":"Test empty-state initialization with two concurrent writers in addition to single-writer failpoints.","principle":"Cross-store initialization must define a single winner when two writers begin from the same empty state.","root_cause":"Marker-first recovery covers a crash, while the current atomic replace writer still permits two no-marker initializers to overwrite each other's UUIDs.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"resolve-tool-session-shape-sequencing","description":"The current function returns only `(session_id, project_id, repo_path)`, so § 3.2's consumer and authorization acceptance cannot pass before its dependent cutover creates the required shape.","finding_id":"PCID-R2-F10","fix":"Make § 3.2 own the final four-column checkout-only function definition and fresh/test schema contracts; keep § 6.1 responsible for applying that same definition transactionally to populated live databases after checkout coverage is proven.","location":"§§ 1.1, 3.2, and 6.1","prevention":"For every SQL result-shape change, place fresh-schema producer, consumer, and live migration in an acyclic order.","principle":"A consumer cannot precede the schema shape it requires, especially when the producer depends on that consumer.","root_cause":"§ 3.2 reads `(machine_id, root_path)`, § 1.1 explicitly leaves `resolve_tool_session` unchanged, and § 6.1 owns the four-column definition while depending on § 3.2.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"soft-deleted-rebind-recovery","description":"An unresolved soft-deleted project cannot complete the required abort-then-rebind-then-rerun flow.","finding_id":"PCID-R2-F11","fix":"Give `rebind` a deleted-inclusive UUID/name resolution path for cutover repair that preserves `deleted_at`, validates the marker, and changes only the owning checkout; add a soft-deleted abort, rebind, and prompt-free rerun test.","location":"§§ 2.3 and 6.1","prevention":"Exercise every campaign inclusion class through abort evidence, the documented repair command, and resume.","principle":"Every row included by a cutover must be addressable by the operator recovery command the cutover prescribes.","root_cause":"§ 6.1 includes soft-deleted ordinary projects, while `gobby projects rebind` uses a resolver that excludes deleted projects.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"cutover-preexisting-coverage-branch","description":"The mandated abort-rebind-rerun flow creates an authoritative checkout before rerun; that rerun has zero unresolved inserts and would abort again under the stated rule.","finding_id":"PCID-R2-F12","fix":"Define two branches: exactly one verified existing checkout means covered with zero inserts; otherwise require exactly one locally validated unresolved candidate. Keep zero or multiple unresolved candidates as errors only in the no-authoritative-row branch, and test both local and foreign-machine rebind recovery.","location":"§ 6.1","prevention":"Enumerate preflight branches for already-covered, one-new, zero-new, and multiple-new states before defining insert cardinality.","principle":"Preflight cardinality rules must distinguish rows already covered from rows requiring insertion.","root_cause":"§ 6.1 calls a verified existing checkout authoritative, then requires exactly one unresolved candidate and says zero unresolved candidates abort.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"9731d596-8931-424a-9151-f31ee553a925","round":2,"round_number":2,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 3 needs_review: accepted all 13 blocking findings with scoped repairs. Aligned C1/1.3 module-function signatures and 1.1 vs 1.2 ownership; pinned GET checkout to the calling-daemon object-or-null payload; mapped isolation/runner acceptance onto existing dedicated tests; split Python index-upsert evidence; added frontend Vitest/type-check/lint; serialize checkout:null via manager.get; owned task_commits/_lifecycle_close plus overlay-aware credential authorization; made primary-versus-overlay upsert mode explicit; specified crash-durable no-clobber marker publication and distinct-root same-name cleanup; defined init restore versus hook non-restore for soft-deleted markers. Did not add an advisory lock, overlay-requires-checkout, wiki-home/USER.md/Tailnet/tenancy, or dual-read.

```json plan-review-round
{"evidence_id":"dc62fbd7-a9bf-4a36-9102-b35fe0b24371","plan_hash":"49ed11b141249298c9de0d0844ef189f86ae33100ee393e862c3075b18aa3e59","round_number":3,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"b7d19d98c4b78b1b96f5573c3f44ffa7774c8b6f9c252fb50300e808dea30b97","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":13,"total":20},"evidence_id":"dc62fbd7-a9bf-4a36-9102-b35fe0b24371","lanes":[{"candidate_count":10,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":5,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"40a61e28f770890bec7e11e4d2bcc331e845dbb4e0525618b3a9d30325cad46a","status":"valid"},"source_digest":"7b7ec4fcfb0da7e4224a53b97f6e815c27771cf2cce13d1ff70cc34fc676ba34","version":1},"findings":[{"category":"traceability","check_key":"checkout-resolver-callable-shape","description":"The plan gives incompatible ownership and signatures for require_root and resolve_operation_root, so independently expanded leaves cannot compile against one stable API.","finding_id":"PCID-R3-F01","fix":"Make the module-level functions canonical: remove require_root from the manager method list, add db to the C1 resolve_operation_root signature, and align all P3/P4 consumer prose.","location":"C1 / §§ 1.2-1.3","prevention":"Compare every public signature in framing sections with the owning deliverable and each named caller before expansion.","principle":"A public resolver contract must expose one consistent callable shape across its owner and consumers.","root_cause":"C1 lists require_root as a LocalProjectCheckoutManager method and omits db from resolve_operation_root, while 1.3 defines both as module functions taking db.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"single-leaf-implementation-ownership","description":"Two sequential leaves claim the checkout manager implementation, and the first leaf lacks the file target needed to perform that work.","finding_id":"PCID-R3-F02","fix":"Delete the Python CRUD sentence from 1.1 and state that 1.2 owns the dataclass and manager; keep 1.1 limited to DDL, generated identities, grants, and schema authorization.","location":"P1 / §§ 1.1-1.2","prevention":"For every implementation sentence, verify exactly one deliverable targets the affected file and owns the acceptance evidence.","principle":"Each changed implementation surface needs one leaf owner with matching Targets.","root_cause":"Section 1.1 assigns the Python dataclass and LocalProjectCheckoutManager CRUD despite omitting their module from Targets; 1.2 separately owns that module and work.","section_id":"1.1","severity":"blocking"},{"category":"missing-requirement","check_key":"checkout-list-http-visibility","description":"The checkout GET route can be implemented as a local row or an all-machine collection, producing materially different exposure of opaque machine paths.","finding_id":"PCID-R3-F03","fix":"Define the route as returning only the calling daemon checkout, with an exact object-or-null payload and errors, and add a two-machine test proving machine B's row is absent from machine A's response.","location":"C1 / § 2.4 / § 5.2","prevention":"For every HTTP route, pin request shape, response shape, caller scope, empty result, and authorization failure.","principle":"A machine-qualified path endpoint needs an explicit response and authorization scope.","root_cause":"GET /api/projects/{project_id}/checkouts is listed without payload or visibility semantics while 5.2 requires caller-specific non-leakage.","section_id":"2.4","severity":"blocking"},{"category":"weak-testability","check_key":"isolation-runner-dedicated-test-seams","description":"Isolation reconciliation and runner wiki-job cleanup can regress while the named live-session-recovery module still passes.","finding_id":"PCID-R3-F04","fix":"Add tests/test_isolation_reconciliation.py and tests/test_runner_project_recovery.py to 3.4 Targets and map 3.4.1 to them; retain the current module for the foreign-session-before-filesystem-I/O case.","location":"P3 / § 3.4","prevention":"Map every production target to the focused test module that already owns its behavior before assigning an acceptance item.","principle":"Acceptance evidence must execute each claimed production surface through its dedicated test seam.","root_cause":"Section 3.4 maps reconciliation and runner-startup cleanup to tests/storage/tasks/test_live_session_recovery.py, whose scope is task/session recovery.","section_id":"3.4","severity":"blocking"},{"category":"weak-testability","check_key":"python-index-upsert-direct-evidence","description":"The Python stale-primary-root guard and overlay exemption have no executable acceptance path.","finding_id":"PCID-R3-F05","fix":"Add tests/code_index/test_storage.py to 4.2 and split 4.2.3 into Rust pipeline coverage plus Python stale-root refusal and overlay-view preservation coverage.","location":"P4 / § 4.2","prevention":"Split cross-language acceptance items and name a focused test target for each implementation.","principle":"A cross-language acceptance item needs direct evidence for each implementation.","root_cause":"Acceptance 4.2.3 includes the Python CodeIndexProjectStorageMixin upsert while citing only a Rust api_contract test.","section_id":"4.2","severity":"blocking"},{"category":"weak-testability","check_key":"frontend-toolchain-validation","description":"The frontend change can pass the stated verification without compiling, linting, or running its named Vitest tests.","finding_id":"PCID-R3-F06","fix":"Add focused Vitest commands for the named web tests plus `npm --prefix web run type-check` and `npm --prefix web run lint` to 5.1 and T1.","location":"P5 / § 5.1 / T1","prevention":"For each implementation_domain and language, include focused tests, type checking, and linting in verification.","principle":"Verification must include the native toolchain for every changed language.","root_cause":"T1 lists Python, Ruff, Mypy, gcode, and gcore checks while 5.1 changes TypeScript and TSX.","section_id":"5.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"nullable-checkout-response-outcome","description":"Projects without a local checkout, including checkout-free sentinels, can fail list/get serialization instead of returning the declared null checkout; frontend null behavior is also unspecified.","finding_id":"PCID-R3-F07","fix":"Use LocalProjectCheckoutManager.get for response serialization, return checkout:null with defined approval_rules and validation_detection defaults, reserve require_root for filesystem operations and settings writes, and test the frontend no-checkout state.","location":"P2 / § 2.4 and P5 / § 5.1","prevention":"Walk null response variants through serialization, derived fields, update routes, and frontend consumers.","principle":"A nullable response branch must remain reachable without invoking a throwing required-value resolver.","root_cause":"The response contract permits checkout:null, while 2.4 directs _project_to_response to load filesystem-derived fields through require_root, which raises for missing checkouts.","section_id":"2.4","severity":"blocking"},{"category":"traceability","check_key":"task-resolver-session-machine-callers","description":"These Git operations can fall back to the daemon machine or lose the current symlink-safe task/ancestor worktree and clone authorization when the resolver changes.","finding_id":"PCID-R3-F08","fix":"Add task_commits.py, tasks/_lifecycle_close.py, and their focused tests to 3.2; thread the verified resolved-session machine_id, preserve explicit descendant and task/ancestor overlay validation, and keep invalid explicit paths as typed failures.","location":"P3 / § 3.2","prevention":"Use gcode callers plus test seams for every changed resolver signature and authorization boundary.","principle":"A machine-qualified resolver migration must update every session-bound caller while preserving its authorization wrapper.","root_cause":"task_commits.py and tasks/_lifecycle_close.py call resolve_task_repo_path or resolve_project_repo_path but are absent from 3.2 Targets.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"primary-overlay-upsert-mode-callers","description":"A generic committed-checkout guard can reject overlays, while a weak implicit distinction can let stale primary writers bypass the guard.","finding_id":"PCID-R3-F09","fix":"Add overlay.rs, sink.rs, direct Rust API tests, and Python storage tests to 4.2; make primary-versus-overlay mode explicit at the upsert boundary, require committed-root equality for primary mode, and keep overlay mode checkout-independent.","location":"P4 / § 4.2","prevention":"For every shared writer, enumerate primary, overlay, retry, and test callers before changing its authorization predicate.","principle":"A shared write API with distinct primary and overlay invariants must expose and test the mode at every caller.","root_cause":"upsert_project_seed and upsert_project_stats serve primary and overlay paths, while overlay.rs, sink.rs, and their direct tests are absent from 4.2.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"crash-durable-marker-no-clobber","description":"The accepted create-if-absent loser-adopts algorithm lacks a crash-durable no-clobber publication primitive.","finding_id":"PCID-R3-F10","fix":"Specify a fully written and fsynced temporary marker followed by atomic no-overwrite installation and directory fsync; the loser discards its temporary file and rereads the winner. Add failpoints for every publication boundary without adding an advisory-lock subsystem.","location":"P2 / § 2.1","prevention":"Enumerate publication failpoints before write, after file sync, after install, and after directory sync.","principle":"A single-winner file publication must expose only complete durable bytes across crashes.","root_cause":"Exclusive creation of the final marker followed by JSON writes can leave an empty or partial marker, while retry treats malformed markers as terminal.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"concurrent-distinct-roots-same-name","description":"Two roots initialized concurrently with the same unused name can leave the losing root's marker UUID without a project row.","finding_id":"PCID-R3-F11","fix":"Add a matrix row and tests for distinct roots with the same name. On name conflict, confirm the marker UUID has no project and the name belongs to another UUID, conditionally remove only the still-matching losing marker, and raise NameAttachRejectedError; retry performs the same cleanup after a crash.","location":"P2 / § 2.1","prevention":"Cross product same-root and different-root concurrency with same and different names, including post-marker failpoints.","principle":"Every uniqueness race must leave each durable marker bound to an existing project row.","root_cause":"The loser-adopts branch covers writers at one root; distinct roots can publish different UUIDs before the active-name uniqueness constraint chooses a database winner.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"managed-credential-overlay-authorization","description":"A valid local worktree or clone request can be rejected even though 3.2 promises overlay precedence; loosening the comparison without registry checks would admit spoofed paths.","finding_id":"PCID-R3-F12","fix":"After verifying session, issuing, and local machine equality, authorize requested_project_path through overlay-aware resolution and add tests for an accepted registered local overlay plus unregistered, wrong-project, and foreign-machine failures.","location":"P3 / § 3.2","prevention":"Trace each requested filesystem path through session identity, machine identity, overlay registry, primary checkout, and spoof rejection.","principle":"Registered overlay precedence must be enforced consistently at credential and filesystem authorization boundaries.","root_cause":"ManagedCredentialManager compares requested_project_path directly with resolve_tool_session's primary root and has no registered-overlay test.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"soft-deleted-marker-transition","description":"A valid marker for a soft-deleted project can report successful registration while the project remains unusable, or silently restore it depending on the wrapper.","finding_id":"PCID-R3-F13","fix":"Define user-invoked init as atomic restore-plus-register for a valid marker, define hook ingress as a typed non-restoring refusal, and test both while preserving rebind's deleted_at behavior.","location":"P2 / §§ 2.1-2.2","prevention":"Include active, soft-deleted, and hard-missing project states in every marker-driven ingress matrix.","principle":"Authoritative identity lookup must define the soft-deleted state transition for each ingress wrapper.","root_cause":"The marker matrix says existing projects register, while ensure_exists preserves deleted_at and hook/init wrappers can therefore produce different restoration behavior.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"2782d871-7ad3-4d13-9c04-4c8e6a8ebf43","round":3,"round_number":3,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 4 needs_review: 12 blocking findings. All accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings.

- PCID-R4-F17 accepted: preparation adds cited-parent:#19670 on #17678 and out-of-scope-for:#19651 on #19670; the downstream chain stays and is not a dependency-closure route.
- PCID-R4-F18 accepted: 1.1.5 and 6.1.10 require catalog, seed, assets.rs, expected-identity, freshness, and schema-contract parity at each schema state.
- PCID-R4-F19 accepted: 4.1 no longer targets managed_postgres_privileges.json; it consumes the 1.1 grant; 6.1 still drops the legacy repo_path grant.
- PCID-R4-F20 accepted: 1.2 targets projects.py for MIGRATED_PROJECT_ID and CHECKOUT_FREE_PROJECT_IDS and pins all four IDs.
- PCID-R4-F21 accepted: 3.3 adds skills-route, Linear CLI, and Linear sync tests for checkout, missing-checkout, and sentinel cases.
- PCID-R4-F22 accepted: 4.2 targets refresh_project_stats and requires explicit primary mode.
- PCID-R4-F23 accepted: all planned calls use the live require_local_machine_id(provided_machine_id, resource_kind, resource_id) signature; no zero-arg helper.
- PCID-R4-F24 accepted: register returns (checkout, created) from the same transaction; HTTP 201/200 follows created, including concurrent same-root.
- PCID-R4-F25 accepted: init restore refuses with NameAttachRejectedError when the soft-deleted name is active on another UUID.
- PCID-R4-F26 accepted scoped as checkout-row FOR SHARE/FOR UPDATE, not an advisory-lock subsystem.
- PCID-R4-F27 accepted: fresh and live resolve_tool_session keep the expired/deleted session filter.
- PCID-R4-F28 accepted: overlay exact-path queries are machine-qualified; foreign-machine same-string overlays do not block a local checkout.

```json plan-review-round
{"evidence_id":"bcf5afcf-d651-474d-84d5-5140cae2384d","plan_hash":"e69134fc48a40b01a111bc14d9fafcad4cc9d58fa92cc30349dc4dcd15f98e53","round_number":4,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"9a01b0228230e9d6d6d036ce4c993d6a35a776054c1b1a55bd0ea5e007e3aa50","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":12,"total":17},"evidence_id":"bcf5afcf-d651-474d-84d5-5140cae2384d","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":6,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":8,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"68fadec720bf4e391d64e3b58aaf84a6c97ea1d9be2f534607a3c056055684e6","status":"valid"},"source_digest":"30686e1e05b825ad7b9b94357ced33f08fd621820f4fe9782d70fbcad26325f6","version":1},"findings":[{"category":"gobby-format","check_key":"deferral-cited-parent-route","description":"D1 still fails typed deferral routing under the plan's own downstream chain.","finding_id":"PCID-R4-F17","fix":"Extend preparation to add and verify out-of-scope-for:#19651 on #19670 before expansion, while preserving #19651 → #18902 → #17678 → #19664.","location":"Constraints / D1","prevention":"For every typed deferral, validate the receiving task's closure or cited-parent route after applying the planned task mutations.","principle":"Typed deferrals require a valid dependency-closure or cited-parent route before expansion.","root_cause":"The prescribed downstream chain places #17678 outside #19651's blocked-by closure, while preparation omits out-of-scope-for:#19651 on cited parent #19670.","section_id":"D1","severity":"blocking"},{"category":"weak-testability","check_key":"generated-schema-acceptance-parity","description":"The derived leaves can close without proving all generated identities match the staged pre-cutover and final schemas.","finding_id":"PCID-R4-F18","fix":"Add acceptance items in 1.1 and 6.1 for catalog and seed manifests, assets.rs identities, schema_expected_identity.json, catalog freshness, and schema-contract parity at each schema state.","location":"§§ 1.1 and 6.1","prevention":"Map every generated schema target to an acceptance item and its freshness or schema-contract test.","principle":"Every required generated schema artifact needs direct acceptance coverage because manifest validation criteria are synthesized only from acceptance items.","root_cause":"Sections 1.1 and 6.1 target regenerated catalog/seed manifests, Rust identities, Python expected identity, and freshness contracts without acceptance items requiring those outputs.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"shared-artifact-single-owner","description":"The privilege manifest has conflicting intermediate owners.","finding_id":"PCID-R4-F19","fix":"Remove crates/gcode/security/managed_postgres_privileges.json from 4.1 and state that 4.1 consumes the grant installed by 1.1; keep legacy grant removal exclusively in 6.1.","location":"§ 4.1","prevention":"Assign each repeated shared target one explicit delta and verify its predecessor dependency.","principle":"A staged shared artifact needs one owner per distinct mutation.","root_cause":"Section 1.1 owns checkout grants and preserves the legacy repo_path grant until 6.1, while 4.1 retargets the same privilege manifest with wording that can remove the legacy grant early.","section_id":"4.1","severity":"blocking"},{"category":"traceability","check_key":"sentinel-constant-target-ownership","description":"The shared sentinel set cannot be implemented within 1.2's declared production targets.","finding_id":"PCID-R4-F20","fix":"Add a justified src/gobby/storage/projects.py::* target to 1.2 for MIGRATED_PROJECT_ID and CHECKOUT_FREE_PROJECT_IDS, then pin all four IDs in tests/storage/test_project_checkouts.py.","location":"§ 1.2","prevention":"Resolve every named existing definition to its file-qualified target before manifest derivation.","principle":"A deliverable must target every production source it changes.","root_cause":"MIGRATED_PROJECT_ID must be defined beside the existing sentinel constants, which live in src/gobby/storage/projects.py, while 1.2 targets only the new checkout module and its test.","section_id":"1.2","severity":"blocking"},{"category":"weak-testability","check_key":"filesystem-consumer-test-seams","description":"Three listed consumer classes can regress while the current 3.3 acceptance remains green.","finding_id":"PCID-R4-F21","fix":"Add tests/servers/routes/test_skills_routes.py, tests/cli/test_linear_coverage.py, and tests/sync/test_linear_sync.py to 3.3 with checkout, missing-checkout, and sentinel coverage.","location":"§ 3.3","prevention":"Pair every filesystem consumer target with local-checkout, missing-checkout, and sentinel test cases.","principle":"Each migrated filesystem consumer class needs a focused executable checkout seam.","root_cause":"Skills routes, Linear CLI, and Linear sync change root resolution in 3.3 while their existing focused tests remain outside Targets and acceptance.","section_id":"3.3","severity":"blocking"},{"category":"traceability","check_key":"upsert-mode-caller-completeness","description":"refresh_project_stats remains outside the primary-versus-overlay migration.","finding_id":"PCID-R4-F22","fix":"Add crates/gcode/src/index/indexer/lifecycle.rs::refresh_project_stats to 4.2 and test primary mode plus committed checkout-root equality.","location":"§ 4.2","prevention":"Run a complete direct-caller sweep for every changed API signature and target each caller symbol.","principle":"Every direct caller of a newly explicit mode parameter must choose a mode.","root_cause":"crates/gcode/src/index/indexer/lifecycle.rs::refresh_project_stats directly calls upsert_project_stats, while 4.2 targets only lifecycle.rs::invalidate.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"local-machine-helper-call-contract","description":"Independent leaves cannot compile against one machine-verification contract.","finding_id":"PCID-R4-F23","fix":"Specify calls as require_local_machine_id(provided_machine_id, resource_kind=\"project_checkout\", resource_id=project_id): pass None when ingress has no claimed machine and pass the HTTP path machine_id for rebind. Add local, missing, and foreign cases.","location":"§§ 1.3, 2.2–2.4, and 3.2","prevention":"Resolve every planned call against the current indexed signature and state any signature change explicitly.","principle":"A shared verification helper needs one exact signature across all planned callers.","root_cause":"The plan repeatedly calls require_local_machine_id() with zero arguments, while the current helper requires provided_machine_id, resource_kind, and resource_id; 1.3 targets the helper without defining a replacement.","section_id":"1.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"atomic-register-created-outcome","description":"The POST status contract has no linearizable implementation seam.","finding_id":"PCID-R4-F24","fix":"Change register to return (checkout, created) from the same INSERT/conflict transaction, drive HTTP status from created, and test concurrent same-root requests with exactly one 201 and remaining successes 200.","location":"§§ 1.2 and 2.4","prevention":"For every create-or-existing API, verify the storage result exposes the committed branch and add a concurrent retry test.","principle":"A status derived from a concurrent mutation must come from that mutation's atomic result.","root_cause":"LocalProjectCheckoutManager.register returns only ProjectCheckout, while HTTP requires 201 for the inserting call and 200 for an idempotent retry; a route-level pre-read permits two concurrent callers to report 201.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"soft-delete-restore-name-collision","description":"The marker-authoritative restore branch is undefined when another active UUID owns the old name.","finding_id":"PCID-R4-F25","fix":"Add a matrix branch that raises NameAttachRejectedError without restoring, registering, or rewriting the marker when the soft-deleted name is active elsewhere; test full transaction rollback.","location":"§ 2.1 marker matrix","prevention":"For each restore, test active-key reuse and require a typed no-mutation outcome.","principle":"Every restore path must account for uniqueness acquired while the row was deleted.","root_cause":"The active-name partial unique index permits another active project to reuse a soft-deleted marker project's name, making unconditional restore fail with an unclassified database uniqueness error.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"stale-primary-writer-linearization","description":"Committed-root equality alone does not establish the claimed stale-writer exclusion.","finding_id":"PCID-R4-F26","fix":"Have Rust and Python primary upserts hold a checkout-row FOR SHARE lock through their state write and have different-root rebind hold FOR UPDATE through checkout update plus active-state deletion. Add paused-writer concurrency tests for seed and stats paths.","location":"§ 4.2","prevention":"For every stale-writer guard, prove both transaction orders using the real write paths and identify the conflicting lock or generation check.","principle":"A check-and-write guard must share a serialization point with the mutation it protects against.","root_cause":"Under READ COMMITTED, a primary writer can validate the old checkout in a statement snapshot, then write old-root active state after rebind updates the checkout and deletes prior state.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","check_key":"resolve-tool-session-status-filter","description":"The planned replacement can widen root resolution to terminal sessions.","finding_id":"PCID-R4-F27","fix":"Require COALESCE(session.status, 'active') NOT IN ('expired', 'deleted') in both definitions and add authorization tests proving expired and deleted sessions return no row.","location":"§§ 3.2 and 6.1","prevention":"Diff each replaced security-definer function's filters, ownership, grants, and session-state behavior before accepting its new shape.","principle":"A security-definer function shape change must preserve every existing authorization predicate unless the plan explicitly removes it.","root_cause":"The live resolve_tool_session excludes expired and deleted sessions, while the 3.2 fresh definition and 6.1 live recreation specify only the checkout join.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"machine-qualified-overlay-detection","description":"The validator's proposed overlay seam violates machine-owned path identity.","finding_id":"PCID-R4-F28","fix":"Target the existing overlay detector, make its exact-path queries machine-qualified, and add tests where a foreign-machine overlay shares the local candidate string while same-machine overlays remain refused.","location":"§§ 1.3 and 2.1","prevention":"Audit every path lookup for machine_id before comparing opaque strings and test cross-machine same-string collisions.","principle":"Opaque path equality is meaningful only inside the owning machine scope.","root_cause":"validate_checkout_root is directed to compose LocalProjectManager._is_registered_isolation_path, which queries worktrees and clones by path without machine_id; a foreign-machine row with the same string can block a valid local checkout.","section_id":"1.3","severity":"blocking"}],"reviewer_session":"225793e3-e7a9-4d96-ad35-2f18083fdb88","round":4,"round_number":4,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 5 needs_review: 7 blocking findings. All accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings.

- PCID-R5-F29 accepted: 4.2.7 requires destination hashing before shared-content adoption and cites the existing serial_db adoption and full-reparse tests.
- PCID-R5-F30 accepted: 1.1 and 6.1 bootstrap install daemon-runtime, migration-owner, and project-and-machine capability SELECT policies; daemon CRUD is tested beside two-machine isolation.
- PCID-R5-F31 accepted scoped as the existing account-identity handoff: no auto-start after target-schema application; predecessor-only abort may start the pre-epic daemon; applied-target abort resumes with staged binaries.
- PCID-R5-F32 accepted scoped: manager register/rebind own the same-txn overlay recheck; all writers persist through those methods; no advisory lock.
- PCID-R5-F33 accepted: overlay_path=None is the only fallback; every non-null invalid overlay is a typed refusal.
- PCID-R5-F34 accepted: require_local_machine_id runs first; foreign-machine rejection happens before filesystem access.
- PCID-R5-F35 accepted scoped: concurrent absent-row rebind is INSERT then on PK conflict FOR UPDATE; no advisory lock.

```json plan-review-round
{"evidence_id":"b852471f-00d2-47ed-904c-b5566e8ffc89","plan_hash":"924205ecda00f3bcf2f2a961fce7b4513fcff0c6cc5d5edcdef77ebcf56820bd","round_number":5,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"321764c8b73c432e2e867d36c3a841f671d5ea7937f4ba2adce7c2856ff19bc8","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":5,"emitted_findings":7,"total":12},"evidence_id":"b852471f-00d2-47ed-904c-b5566e8ffc89","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":7,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"d82c5512becd1b37b8d2a49ad62014d5a9d2270c566b87e9632b7d1634d61d7e","status":"valid"},"source_digest":"2abe7dcb31420118c410203f0f6198886297c27e9a7078ae620811c23742c43c","version":1},"findings":[{"category":"weak-testability","check_key":"shared-content-adoption-acceptance","description":"Section 4.2 requires destination discovery and hashing before shared-content adoption, and T1 repeats it, while acceptance items 4.2.1 through 4.2.6 omit that behavior. The derived leaf can close without running the existing serial_db adoption test.","finding_id":"PCID-R5-F29","fix":"Add a section 4.2 acceptance item requiring destination hashing before shared-content adoption and cite crates/gcode/src/index/indexer/tests/serial_db.rs::indexing_adopts_existing_content_version_without_reparse. Keep the full-index reparse case explicit as the adjacent non-adoption variant.","location":"P4 / section 4.2","prevention":"Map every imperative verification bullet and runtime invariant to a deliverable acceptance item with its executable test seam.","principle":"Every required runtime invariant must appear in the acceptance criteria that become leaf validation.","root_cause":"Destination hashing before shared-content adoption remains only in deliverable prose and T1, outside the acceptance items used to derive the manifest.","section_id":"4.2","severity":"blocking"},{"category":"missing-requirement","check_key":"checkout-rls-policy-coverage","description":"Baseline RLS policies are generated from hard-coded relation inventories. The plan does not explicitly require project_checkouts daemon-runtime and migration-owner policies or a project-and-machine capability policy, and the predecessor bootstrap can create the forced-RLS table before those policies exist.","finding_id":"PCID-R5-F30","fix":"Require 1.1 and the 6.1 bootstrap to install and verify daemon-runtime and migration-owner access plus SELECT capability policy constrained by project_id and current_machine_id(). Add direct daemon CRUD coverage alongside the existing two-machine capability isolation test.","location":"P1 / section 1.1 and P6 / section 6.1","prevention":"For each FORCE RLS relation, inventory daemon, migration, capability, read, and write roles across fresh schema and live bootstrap.","principle":"A FORCE RLS table needs explicit policies for every required execution role in every schema installation path.","root_cause":"The plan specifies the new table, grants, and capability isolation while omitting the hard-coded daemon and migration policy inventories from fresh-schema and campaign-bootstrap acceptance.","section_id":"1.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"cutover-daemon-restart-handoff","description":"hub-maintenance currently starts the installed daemon after successful release and on abort. After projects.repo_path is dropped and resolve_tool_session changes shape, either path can launch the pre-epic daemon against the target schema.","finding_id":"PCID-R5-F31","fix":"Add a 6.1 handoff that bypasses automatic restart after target-schema application, selects the matching staged gdaemon and gcode binaries, then performs manual start and health verification. Define predecessor-only abort as the old-daemon restart case and applied-target abort as resume or staged-binary recovery, with focused CLI tests for both.","location":"P6 / section 6.1","prevention":"Audit stop, success, abort, resume, release, and restart transitions whenever a campaign changes a live function or drops a column.","principle":"A destructive schema cutover may restart service only with binaries proven compatible with the committed target schema.","root_cause":"The plan requires matching staged binaries during cutover while leaving existing hub-maintenance success and abort restart paths unchanged.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"overlay-recheck-all-writers","description":"Hook registration, CLI rebind and repair, HTTP register and rebind, and create, ensure, and update can race an overlay registry insert after validate_checkout_root returns. Only section 2.1 requires the settled same-transaction recheck.","finding_id":"PCID-R5-F32","fix":"Make the exact machine-qualified overlay recheck a LocalProjectCheckoutManager register and rebind transaction precondition while retaining caller-side filesystem validation. Extend hook, CLI, HTTP, create, ensure, and update tests to prove the recheck occurs in the same transaction, with the settled no-advisory-lock design.","location":"P1 / sections 1.2-1.3 and P2 / sections 2.2-2.4","prevention":"Enumerate every checkout-establishing writer and verify the same machine-qualified overlay query runs within its mutation transaction.","principle":"Every writer enforcing a cross-table exclusion must repeat the exclusion check inside its write transaction.","root_cause":"The transaction-local overlay recheck is attached only to init, while other ingress paths rely on caller-side filesystem validation before SQL-only register or rebind.","section_id":"1.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"invalid-overlay-resolution-contract","description":"resolve_operation_root can silently operate on the primary checkout when its caller supplied an invalid overlay, even though the acceptance contract says that overlay must be refused. Implementers cannot satisfy both contracts.","finding_id":"PCID-R5-F33","fix":"Make overlay_path=None the sole fallback branch. Return a registered local overlay when valid, and raise a typed refusal for every non-null unregistered, wrong-project, or foreign-machine overlay. Align C1, section 1.3 prose, acceptance 1.3.4, and explicit-path consumers.","location":"P1 / section 1.3","prevention":"For optional path overrides, test absent, valid, unregistered, wrong-owner, wrong-project, and foreign-machine branches against one stated outcome each.","principle":"An explicit operation root override needs one unambiguous invalid-input outcome.","root_cause":"C1 and section 1.3 prose specify primary-checkout fallback for an invalid non-null overlay_path, while acceptance 1.3.4 specifies typed refusal.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"machine-context-before-validation","description":"The P2 instructions cannot call validate_checkout_root with its declared signature before resolving machine_id. On HTTP rebind, this order can inspect the local path before rejecting a foreign path machine_id.","finding_id":"PCID-R5-F34","fix":"Call require_local_machine_id(provided_machine_id, resource_kind=\"project_checkout\", resource_id=project_id) first for every P2 ingress, pass that single returned ID into validate_checkout_root and register or rebind, and require foreign-machine rejection before filesystem access in focused tests.","location":"P2 / sections 2.2-2.4","prevention":"For every ingress, trace identity resolution, filesystem access, validation, and mutation in call order using the live helper signature.","principle":"Ownership context must be resolved before any machine-scoped filesystem validation.","root_cause":"P2 orders validate_checkout_root before require_local_machine_id even though the validator requires machine_id for overlay queries.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"empty-row-rebind-linearization","description":"Two rebind calls can both observe an absent checkout. No checkout row exists to lock, so the losing insert can surface an untyped primary-key violation or fail to enter the different-root branch that owns index invalidation.","finding_id":"PCID-R5-F35","fix":"Define an atomic absent-row protocol: attempt INSERT, then on primary-key conflict re-read the checkout row FOR UPDATE and execute the same-root or different-root branch, translating root ownership conflicts. Add concurrent absent rebind tests for equal and different roots while retaining checkout-row locking and the settled no-advisory-lock design.","location":"P1 / section 1.2 and P4 / section 4.2","prevention":"For every row-locked mutation, test concurrent transitions from absent, same, and different states with typed losing outcomes.","principle":"A row-lock state machine needs an explicit linearization rule for the state where the lockable row does not yet exist.","root_cause":"The rebind protocol names checkout-row FOR UPDATE only for an existing different-root row and leaves concurrent absent-row inserts to unique-constraint behavior.","section_id":"4.2","severity":"blocking"}],"reviewer_session":"9ba606ed-7acc-46cd-8c20-4f5322105a5c","round":5,"round_number":5,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 6 needs_review: 6 blocking findings. All accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. Not fixer-prose of R1–R5.

- PCID-R6-F36 accepted: add account_identity_cutover.py to 6.1; _TARGET_CAMPAIGNS and known-constraint recognition preserve project-checkout-cutover.
- PCID-R6-F37 accepted: App.test.tsx, SkillsTab.test.tsx, and WikiA11y.test.tsx move to 5.1 Targets and checkout-shaped fixtures.
- PCID-R6-F38 accepted scoped: name the fifteen leftover SQL fixtures in 6.1 Targets; keep the single residue-sweep acceptance; no new leaves.
- PCID-R6-F39 accepted: 2.3 repair matrix gives one outcome each for missing same-root register versus typed refuse.
- PCID-R6-F40 accepted: no-marker CheckoutRootTakenError after marker publication unlinks the still-matching marker.
- PCID-R6-F41 accepted: cutover persists through register after require_local_machine_id and validate_checkout_root; no advisory lock.

```json plan-review-round
{"evidence_id":"64da4b75-c400-4626-ad30-4dc77f18f855","plan_hash":"76b830a15fa111e10628da99be21d1c5b2fb06031bec5bdb5c076b2dbfb63ed4","round_number":6,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2c78e633bd3e9d59a96fc65e9a67c3095dc5e883200247737245572208178a3c","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":6,"total":7},"evidence_id":"64da4b75-c400-4626-ad30-4dc77f18f855","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"74abef7cf606e308e2d53e240cafb09ba86858a8c9d71307f2108fde8fc1c5a0","status":"valid"},"source_digest":"1697d49eb8326f5ae2fc229a927667299ad564880b3a067a038aa04eebc34148","version":1},"findings":[{"category":"traceability","check_key":"campaign-registry-consumer-parity","description":"The existing account-identity cutover can reject the expanded constraint as unknown or recreate a constraint that excludes project-checkout-cutover, despite 6.1.7 passing against only the main registry and baseline.","finding_id":"PCID-R6-F36","fix":"Add src/gobby/storage/account_identity_cutover.py::* to 6.1. Update its _TARGET_CAMPAIGNS and known-constraint recognition to preserve project-checkout-cutover, and extend tests/storage/test_account_identity_cutover.py to prove constraint replacement retains the expanded set.","location":"P6 / section 6.1","prevention":"For each new enum-like campaign value, search every constructor, exhaustive match, constraint validator, and constraint rebuilder and add each production consumer to Targets.","principle":"Every exhaustive campaign registry and constraint rebuilder must move with a new campaign value.","root_cause":"Section 6.1 updates the main Campaign/CAMPAIGNS registry and baseline constraints but omits account_identity_cutover.py, whose hard-coded target set both recognizes and rebuilds those constraints.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"frontend-fixture-shape-parity","description":"web/src/__tests__/App.test.tsx, web/src/components/activity/skills/__tests__/SkillsTab.test.tsx, and web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx still construct or return repo_path and are absent from 5.1 Targets.","finding_id":"PCID-R6-F37","fix":"Add all three files to 5.1 Targets and focused validation. Replace their logical project repo_path fields with checkout objects or checkout: null as appropriate.","location":"P5 / section 5.1","prevention":"Run a field-level blast-radius sweep across frontend source and tests, then place every remaining constructor or response mock in the owning leaf's Targets.","principle":"A response-shape cutover must target every typed constructor and logical HTTP fixture that encodes the removed field.","root_cause":"The frontend target inventory covers several ProjectWithStats fixtures but misses three current project-response fixtures still carrying repo_path.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"schema-drop-fixture-inventory-parity","description":"Fifteen current SQL fixtures remain outside 6.1 Targets: tests/cli/test_import.py; tests/mcp_proxy/test_metrics_manager.py; tests/mcp_proxy/test_metrics_store.py; tests/mcp_proxy/test_registries.py; tests/mcp_proxy/tools/test_apply_persona.py; tests/mcp_proxy/tools/test_hub.py; tests/mcp_proxy/tools/workflows/test_import.py; tests/memory/test_manager.py; tests/plans/test_plan_coverage_ci.py; tests/sessions/test_e2e_session_tracking.py; tests/sessions/test_token_usage.py; tests/storage/test_checkpoints.py; tests/storage/test_manager_surface_parity.py; tests/storage/test_task_affected_files.py; and tests/workflows/test_pipeline_heartbeat.py.","finding_id":"PCID-R6-F38","fix":"Add those fifteen files to 6.1 Targets. For each fixture, remove the unused repo_path column or create a verified machine/project_checkouts row when the test exercises filesystem behavior; keep the existing single residue-sweep acceptance rather than creating new leaves.","location":"P6 / section 6.1","prevention":"Before finalizing a drop-column leaf, run the residue query, subtract already-owned files, and list every remaining direct SQL fixture under Targets.","principle":"A dropped column requires every direct SQL fixture that names it to be declared and migrated in the column-drop leaf.","root_cause":"The accepted residue sweep exists, but the 6.1 target inventory names only two of the seventeen current test files whose INSERT INTO projects statements include repo_path.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"state-branch-single-outcome","description":"The missing-checkout repair branch is contradictory, so an implementation cannot satisfy both the prose and acceptance 2.3.3 deterministically.","finding_id":"PCID-R6-F39","fix":"Replace the refusal sentence with an explicit repair matrix: a missing row with a valid same-root marker registers and reports creation; overlay, sentinel, marker mismatch, invalid root, and conflicting existing-row cases refuse without persistence. Test each outcome.","location":"P2 / section 2.3","prevention":"For every mutation, enumerate absent, same, conflicting, invalid, and sentinel states and assign exactly one transition and result to each.","principle":"Each reachable state-machine branch must specify one observable outcome.","root_cause":"Section 2.3 first allows repair to register a validated missing same-root checkout, then classifies missing-checkout repair as a typed refusal with no persistence.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"marker-publication-root-taken-recovery","description":"A no-marker init can publish a new UUID and then lose project-plus-checkout registration because that machine/root is already owned by another project, leaving a durable marker whose UUID has no project row and a retry that repeats the same failure.","finding_id":"PCID-R6-F40","fix":"Add the no-marker/root-already-owned branch and its race variant. After confirming the published UUID has no project row and another project owns the machine/root, unlink only this attempt's still-matching marker, fsync the directory, and raise CheckoutRootTakenError. Add failpoints and retry tests around rollback, unlink, and directory fsync.","location":"P2 / section 2.1","prevention":"Enumerate every post-publication transaction failure and prove the marker either resolves to a committed project row or is conditionally removed durably.","principle":"Marker-first publication needs a cleanup or retry transition for every permanent database rollback that leaves the published UUID without a project row.","root_cause":"The plan defines conditional marker cleanup for the active-name loser but omits the analogous CheckoutRootTakenError path after a newly published marker.","section_id":"2.1","severity":"blocking"},{"category":"traceability","check_key":"checkout-writer-guard-parity","description":"Cutover is the sole planned checkout writer that does not explicitly verify the candidate machine before filesystem access or retain register's same-transaction overlay recheck, so a race can violate the invariant enforced everywhere else.","finding_id":"PCID-R6-F41","fix":"Require each unresolved candidate to call require_local_machine_id(candidate_machine_id, resource_kind=\"project_checkout\", resource_id=project_id) before filesystem access, pass the returned ID to validate_checkout_root, and persist through LocalProjectCheckoutManager.register inside the campaign's ambient transaction. Assert register.created exactly matches the recorded insert set.","location":"P6 / section 6.1","prevention":"Inventory every production checkout writer, including maintenance campaigns, and require the common helper/validator/manager sequence before accepting its write path.","principle":"Every checkout-establishing writer must use the same machine-verification, filesystem-validation, and transaction-local overlay guard pipeline.","root_cause":"Section 6.1 validates candidates and then specifies direct inserts, leaving the campaign outside the settled require_local_machine_id → validate_checkout_root → LocalProjectCheckoutManager.register contract.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"97f62b22-adcf-496f-9204-0f74f21e9aaa","round":6,"round_number":6,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 7 needs_review: accepted F42 (nine Playwright project fixtures into 5.1), F43 scoped to tests/servers/conftest.py only (declined the 21-file create() inventory; 1.3 already defers other callers), F44 scoped to a lock-only UPDATE grant (declined a SECURITY DEFINER helper), F45 scoped to per-write FOR SHARE plus committed-root equality (declined one pipeline transaction), F46 absent-row rebind clears mismatched recorded-root index state, F47 3.2 regenerates assets.rs and schema_expected_identity.json, F48 list_projects plus useFiles.test.ts, F49 remove unused list_for_project. Did not add an advisory lock, wiki-home/USER.md/Tailnet/tenancy, dual-read, or overlay-requires-checkout.

```json plan-review-round
{"evidence_id":"392d916c-c115-4cab-be1c-a40a16bd85eb","plan_hash":"01f4d655afbdf92a6a25526b3bbe4b1201134239269919ddc89708c4d41ff258","round_number":7,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"0a16d2caa70bd932c929a777890de66a40abd9fe40a7daa86892201b04bdbee2","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":8,"total":9},"evidence_id":"392d916c-c115-4cab-be1c-a40a16bd85eb","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":4,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"040c970db02f93a5ef12b4558457cea8cb61341d0aab423979da43eea0f6e167","status":"valid"},"source_digest":"3f19ee04d35624491ef3222beb62335d6392dd6b8a10c7d4ef4c4aab07b5fa6f","version":1},"findings":[{"category":"traceability","check_key":"frontend-fixture-shape-parity","description":"Nine current Playwright fixtures remain outside § 5.1 and § 6.1 Targets while constructing logical project JSON with repo_path. The residue gate would force undeclared edits, and these tests would retain the removed response contract.","finding_id":"PCID-R7-F42","fix":"Add web/tests/activity-panel-changes-session-scope.spec.ts, activity-panel-web-chat-sessions.spec.ts, epic-10452-verification.spec.ts, file-editor.spec.ts, provider-picker.spec.ts, style-surfaces.spec.ts, terminal-colors.spec.ts, web-chat-restore-plan.spec.ts, and web-chat-swap-send-respond.spec.ts to § 5.1 Targets and focused validation. Convert logical project fixtures to checkout object-or-null while preserving unrelated worktree repo_path fields.","location":"Phase 5 / § 5.1","prevention":"Run an exhaustive logical-project JSON fixture sweep and distinguish those fixtures from worktree responses before finalizing Targets.","principle":"A response-shape cutover must target every constructor and logical HTTP fixture encoding the removed field.","root_cause":"The frontend target inventory covers selected unit fixtures while omitting nine Playwright project-response fixtures that still emit project-level repo_path.","section_id":"5.1","severity":"blocking"},{"category":"traceability","check_key":"python-project-fixture-inventory","description":"The indexed sweep found 42 project_manager.create(..., repo_path=...) calls across 21 untargeted test files, plus direct update and Project-row fixtures. Shared helpers such as tests/conftest.py and tests/servers/conftest.py currently establish roots without the new machine, marker, and checkout preconditions.","finding_id":"PCID-R7-F43","fix":"Add the affected shared helpers and indexed caller set to § 1.3 or § 6.1 Targets within the existing leaves. Migrate filesystem tests to a verified machine-marker-project-checkout fixture, omit the path argument where filesystem behavior is irrelevant, remove repo_path from Project rows, and extend the single residue assertion to cover manager callsites.","location":"Phase 1 / § 1.3","prevention":"Sweep LocalProjectManager create, update, and ensure_exists callsites plus direct Project constructions whenever a stored identity field is removed.","principle":"A constructor contract migration must declare every caller, fake, and shared fixture that enters the new validation path.","root_cause":"The target inventory names raw-SQL residue fixtures while omitting manager create/update callers and Project-row fixtures affected by the removal of projects.repo_path.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"capability-row-lock-privilege","description":"Rust primary upserts cannot acquire the specified project_checkouts row lock under the § 1.1 SELECT-only capability. Primary indexing would fail before it can validate or write active state.","finding_id":"PCID-R7-F44","fix":"Add a narrowly scoped SECURITY DEFINER helper owned by § 1.1 that validates the bound project and machine, acquires the shared checkout-row lock in the caller's transaction, and returns the committed root without granting direct checkout mutation. Route § 4.2 primary fencing through it and add real-capability seed and stats tests.","location":"Phase 4 / § 4.2","prevention":"Exercise every capability-scoped locking statement through the real managed role during grant design.","principle":"Every planned lock must be executable under the deployed role's actual grant set.","root_cause":"The plan combines a SELECT-only project_checkouts capability with direct SELECT ... FOR SHARE, which requires UPDATE privilege on the locked relation.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"primary-file-state-mutation-fence","description":"A rebind can commit after seed, clear old active state, and then an old-root pipeline can recreate code_indexed_file_states before its final stats guard fails. Those file rows survive the failed run.","finding_id":"PCID-R7-F45","fix":"Keep discovery and hashing before the database transaction, then hold one shared checkout fence from before the first primary active-state write through seed, file-state upsert/adopt/delete, orphan cleanup, and final stats in one transaction. Preserve checkout-independent overlay writes and add races paused before each file-state mutation.","location":"Phase 4 / § 4.2","prevention":"Enumerate and race-test every active-state write between checkout validation and final publication.","principle":"A stale-root fence must cover every persistent mutation that can recreate the invalidated view.","root_cause":"The plan locks seed and final stats while current per-file upsert, adoption, deletion, and cleanup statements execute between them as separately committed writes.","section_id":"4.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"absent-rebind-index-state-branch","description":"Before cutover, a machine/project can have active project and file index state while lacking a checkout row. Rebinding that project to a different root establishes the new checkout while retaining legacy-root active state.","finding_id":"PCID-R7-F46","fix":"Specify that absent-row rebind preserves active state only when no state exists or its recorded root matches the inserted checkout. When existing primary state records another root, insert the checkout and clear that machine/project's active project and file states in the same transaction; cover pre-cutover repair and abort-rebind-rerun.","location":"Phase 4 / § 4.2","prevention":"Test rebind's absent-row branch with no state, same-root state, and different-root state before migration rollout.","principle":"Every rebind branch must reconcile pre-existing active state with the root it makes authoritative.","root_cause":"Absent-row rebind always inserts without considering active index state left by the pre-cutover schema.","section_id":"4.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"intermediate-baseline-generated-identity","description":"Replacing resolve_tool_session in baseline.sql changes the baseline checksum. The § 3.2 leaf omits crates/gcore/src/schema/assets.rs and src/gobby/storage/schema_expected_identity.json, leaving its fresh/test schema identity stale.","finding_id":"PCID-R7-F47","fix":"Add crates/gcore/src/schema/assets.rs and src/gobby/storage/schema_expected_identity.json to § 3.2 Targets and add acceptance to regenerate and verify the intermediate identity after installing the four-column resolve_tool_session definition.","location":"Phase 3 / § 3.2","prevention":"For every baseline.sql Target, require the embedded Rust checksum and Python expected-identity files in the same leaf.","principle":"Each baseline mutation leaf must leave every generated checksum and expected schema identity synchronized.","root_cause":"Section 3.2 changes baseline.sql after § 1.1's generation checkpoint and before § 6.1's final checkpoint without owning the generated identity consumers.","section_id":"3.2","severity":"blocking"},{"category":"weak-testability","check_key":"files-checkout-response-seam","description":"src/gobby/servers/routes/files.py::list_projects still returns repo_path and web/src/hooks/useFiles.ts declares it. The existing web/src/hooks/__tests__/useFiles.test.ts is absent from Targets, so the path-free checkout object-or-null contract lacks a focused end-to-end seam.","finding_id":"PCID-R7-F48","fix":"Make § 3.3 explicitly cut list_projects over to checkout-shaped project JSON and add backend route assertions. Add web/src/hooks/__tests__/useFiles.test.ts to § 5.1 Targets with checkout-object and checkout-null cases.","location":"Phase 3 / § 3.3 and Phase 5 / § 5.1","prevention":"Trace each frontend response interface to its backend producer and colocated hook test before accepting a shape migration.","principle":"An HTTP shape cutover must update its producer and typed consumer together with focused object and null response tests.","root_cause":"The files project-list endpoint and useFiles retain the legacy repo_path contract, while § 5.1 targets useFiles without its existing focused test seam.","section_id":"5.1","severity":"blocking"},{"category":"over-engineering","check_key":"unconsumed-checkout-list-method","description":"Section 1.2 specifies and tests list_for_project even though all plan consumers use get, list_for_machine, register, or rebind. The extra query adds unused API and test ceremony.","finding_id":"PCID-R7-F49","fix":"Remove list_for_project and its acceptance coverage from § 1.2. Retain get, list_for_machine, register, and rebind until a concrete cross-machine project-list consumer exists.","location":"Phase 1 / § 1.2","prevention":"Map every proposed public manager method to at least one deliverable consumer before retaining it.","principle":"A new manager method earns its surface through a concrete consumer in the plan.","root_cause":"LocalProjectCheckoutManager.list_for_project has no named HTTP, CLI, cutover, startup, or resolver caller.","section_id":"1.2","severity":"nit"}],"reviewer_session":"097c9186-6e0a-4da5-8d70-bfaabd441bda","round":7,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 8 needs_review: accepted F50 scoped (family test seams in 3.1/3.2/3.3; declined a 21-file create() inventory), F51 content_gc adopt_file_state caller, F52 three task-lifecycle mocks plus github_issue_sync leftover in 6.1, F53 overlay-refusal post-publication marker cleanup (same-txn recheck retained), F54 5.1 depends on 3.3, F55 gcode name lookup is active-only. Did not add an advisory lock, wiki-home/USER.md/Tailnet/tenancy, dual-read, or overlay-requires-checkout.

```json plan-review-round
{"evidence_id":"1275b405-179d-4569-ac34-9eda89e5bf10","plan_hash":"bd292ec469045f1c5e8a24cfe3c8cb376ec697981f829da532f6ff38b58b1c3b","round_number":8,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"fe8d444fa3dd755aa2931bc48fc1a77ad9abc9649538feb80d8b958be98db580","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":6,"total":8},"evidence_id":"1275b405-179d-4569-ac34-9eda89e5bf10","lanes":[{"candidate_count":3,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":3,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"53a989917279fc73472869a1476eb4bb851266914f6f3d81a44e5e8169e94844","status":"valid"},"source_digest":"1a75dc1307dee38b7683f81e5f2057de50cb83a1c95daacb98dcca592bb5b6a9","version":1},"findings":[{"category":"weak-testability","check_key":"consumer-family-test-evidence","description":"The cited build-input, task-path, and files-route tests do not execute several production families whose root behavior those acceptance items claim, leaving dispatch, lifecycle-monitor, middleware/WebSocket, plan/workflow/wiki/memory, and scheduler regressions unobservable.","finding_id":"PCID-R8-F50","fix":"Add tests/dispatch/test_dispatcher.py and tests/dispatch/test_workspace_merge.py to 3.1; add tests/agents/test_lifecycle_monitor.py, tests/servers/test_project_context_middleware.py, tests/servers/websocket/chat/test_session.py, tests/mcp_proxy/tools/spawn_agent/test_factory.py, and tests/mcp_proxy/test_mcp_tools_session_messages.py to 3.2; split 3.3.1 by its remaining consumer families and target their focused existing tests. Each family must exercise checkout success plus its relevant overlay and missing or foreign failure branches.","location":"P3 / sections 3.1, 3.2, and 3.3","prevention":"For each broad cross-cutting acceptance item, map every consumer family to a focused test target and cover local checkout, overlay, and missing or foreign context branches.","principle":"Each claimed consumer family needs direct acceptance evidence at its own runtime boundary.","root_cause":"Sections 3.1.1, 3.2.1, and 3.3.1 use one narrow test artifact apiece to attest many unrelated dispatch, agent, session, HTTP, plan, workflow, wiki, memory, and scheduler consumers.","section_id":"3.2","severity":"blocking"},{"category":"traceability","check_key":"file-state-direct-caller-parity","description":"crates/gcode/src/commands/status/content_gc/tests.rs directly calls adopt_file_state with the current checkout-unaware signature. Section 4.2 changes that boundary and omits this caller, so the leaf can leave a broken or semantically unclassified test seam.","finding_id":"PCID-R8-F51","fix":"Add crates/gcode/src/commands/status/content_gc/tests.rs to 4.2 Targets. Make both direct adoption cases select the intended mode explicitly and seed a matching checkout for every primary case.","location":"P4 / section 4.2","prevention":"Run an exact direct-caller sweep for every changed active-state writer and place each caller or fake in the owning deliverable.","principle":"Every direct caller of a changed state-write API must declare the new primary or overlay contract.","root_cause":"The direct adopt_file_state calls in the content-GC test module are outside 4.2 Targets even though adoption gains checkout locking and primary-versus-overlay behavior.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"project-repo-path-fake-parity","description":"tests/mcp_proxy/tools/test_task_lifecycle_coverage.py, test_tasks_lifecycle_coverage.py, and test_task_worktree_lifecycle_decoupling.py still mock Project.repo_path for task-root resolution, while tests/sync/test_github_issue_sync.py still assigns the removed logical field. None is targeted.","finding_id":"PCID-R8-F52","fix":"Add the three task-lifecycle files to 3.2 Targets and replace their repo_path-only project mocks with session-machine checkout or resolver fakes. Add tests/sync/test_github_issue_sync.py to 6.1 Targets and remove its obsolete Project.repo_path assignment.","location":"P3 / section 3.2 and P6 / section 6.1","prevention":"After changing a project field, sweep constructor keywords, MagicMock attributes, assignments, and resolver fakes, then assign each concrete file to its owning leaf.","principle":"A removed identity field requires every project fake and assignment to move in its owning leaf.","root_cause":"The target sweep covered selected task and sync tests while missing adjacent suites that still supply project identity only through Project.repo_path.","section_id":"3.2","severity":"blocking"},{"category":"unhandled-edge","check_key":"post-publication-overlay-refusal-recovery","description":"A no-marker initializer can publish its UUID, then lose the same-transaction overlay recheck with OverlayRegistrationRejectedError. The project transaction rolls back and the plan leaves a durable marker whose UUID has no project row.","finding_id":"PCID-R8-F53","fix":"Retain the settled same-transaction overlay recheck. For its post-publication refusal branch, confirm the UUID has no project row, unlink only this attempt's still-matching marker, fsync the directory, and raise OverlayRegistrationRejectedError. Add failpoints after rollback, unlink, and directory fsync plus a retry test.","location":"P2 / section 2.1","prevention":"Enumerate every exception reachable after marker installation and classify it as retryable-marker retention or conditional durable cleanup.","principle":"Marker-first publication needs a recovery transition for every permanent rollback after publication.","root_cause":"The accepted transaction-local overlay recheck can refuse registration after the new marker is durable, while cleanup is specified only for name and root-ownership losers.","section_id":"2.1","severity":"blocking"},{"category":"bad-sequencing","check_key":"files-response-producer-consumer-order","description":"5.1 depends only on 2.4 even though useFiles consumes GET /api/files/projects, whose checkout object-or-null cutover belongs to 3.3. Expansion may schedule the typed consumer before its producer contract exists.","finding_id":"PCID-R8-F54","fix":"Change 5.1 dependencies to include 3.3 alongside 2.4.","location":"P5 / section 5.1","prevention":"Trace every response-shape consumer to its producer leaf and encode that edge before allowing parallel expansion.","principle":"A typed response consumer depends on the deliverable that establishes its producer contract.","root_cause":"Section 5.1 changes useFiles to the checkout-shaped files-project response, while section 3.3 owns that endpoint and is absent from 5.1 dependencies.","section_id":"5.1","severity":"blocking"},{"category":"missing-requirement","check_key":"gcode-name-resolution-deleted-ambiguity","description":"resolve_project_by_name can match multiple same-name soft-deleted projects with local checkout rows, or a deleted row alongside the active project. Section 4.1 leaves selection and ambiguity behavior undefined despite the governing requirement for explicit missing or ambiguous identity errors.","finding_id":"PCID-R8-F55","fix":"Specify active-only gcode name lookup with projects.deleted_at IS NULL. Add tests proving the single active checkout wins when deleted duplicates exist, deleted-only names return a miss, and exact marker or UUID resolution remains the explicit path for non-name identity.","location":"P4 / section 4.1","prevention":"For every name resolver, test active, deleted-only, active-plus-deleted, and multiple-deleted states against one stated outcome.","principle":"Name resolution over soft-deletable rows must define deletion and ambiguity semantics.","root_cause":"Projects enforce uniqueness only for active names, soft-deleted projects may retain repaired checkouts, and 4.1 specifies a name join without a deleted-row policy.","section_id":"4.1","severity":"blocking"}],"reviewer_session":"66d34fc3-7e95-4ee3-8e44-b19e31dcb036","round":8,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 9 needs_review: 1 blocking finding. Accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. First Round 9 attempt 738cc132 expired as stuck/no-attestation and does not count.

- PCID-R9-F56 accepted: C1 and #18902 grant shape is projects SELECT (id, name, deleted_at); 1.1 adds the baseline/privilege-manifest column and live capability predicate; 4.1 consumes that grant without retargeting the privilege manifest; 6.1 keeps deleted_at when repo_path is dropped.

```json plan-review-round
{"evidence_id":"abae0c27-cf95-4eab-afaf-30e65222bc99","plan_hash":"d7f1f2247173789251e3485cc6d69fc60fc0c9cc15c5b315125e6abc51e0f421","round_number":9,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"2cd85bd3e912c80582f3d9888200c1a8935f5a1737543f551a8880c988de1885","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":1,"total":3},"evidence_id":"abae0c27-cf95-4eab-afaf-30e65222bc99","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"fda543bd4a550c53e816d8b3bfacc9e288ecd962362b5f7cbf6558cd2855fddf","status":"valid"},"source_digest":"47e5d4b673e885717370478b01f324af5d47ebae40b61ea40778bd88d4c75338","version":1},"findings":[{"category":"missing-requirement","causal_finding_id":"PCID-R8-F55","causal_section_ids":["4.1","T1"],"check_key":"gcode-active-name-grant-column-parity","description":"The active-only gcode name lookup now filters projects.deleted_at, but gobby_gcode_capability can select only projects.id and projects.name. PostgreSQL checks SELECT privilege for columns used in WHERE, so capability-backed name resolution will fail before it can apply the active-only policy.","finding_id":"PCID-R9-F56","fix":"Add deleted_at to the C1 and #18902 projects grant shape; update 1.1 baseline grants, managed_postgres_privileges.json, privilege-parity coverage, and live capability authorization tests; keep that column grant through 6.1 when repo_path is removed; and state in 4.1 that active-name resolution consumes the expanded grant.","introduced_in_round":8,"location":"C1 / P1 section 1.1 / P4 section 4.1 / P6 section 6.1","prevention":"For each capability query change, compare every projected, joined, and filtered column against the managed column grant and exercise the query through the real capability role.","principle":"Every SQL predicate executed under a column-scoped grant must have SELECT privilege on each referenced column.","root_cause":"Round 8 added projects.deleted_at IS NULL to gcode name resolution while C1, 1.1, and 6.1 retained the capability grant as projects SELECT (id, name) only.","section_id":"1.1","severity":"blocking"}],"reviewer_session":"2bead0d2-cbb4-40cb-adc3-6bede55226e3","round":9,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 10 needs_review: 3 blocking findings. All accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. Tenth and final counted round.

- PCID-R10-F57 accepted: T1 rebind verification now distinguishes same-root and matching-or-empty absent-row (no invalidation) from mismatched recorded-root absent-row (clear that machine/project's active project and file states).
- PCID-R10-F58 accepted: 6.1 owns privilege-manifest parity after dropping repo_path; final projects columns are id, name, and deleted_at.
- PCID-R10-F59 accepted: cutover coverage is per (machine_id, project_id); one or more existing rows are authoritative and preserved; two-machine existing rows insert nothing.

```json plan-review-round
{"evidence_id":"47fa5bac-95a5-4350-8d7f-39b416f8f055","plan_hash":"cdcd7ab468909923ff1832b498f1a2ddc30032f74fb989e16b2f3635166bc0d7","round_number":10,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"b018f169e30c1779bd60a10ee91cf6f01e2254c5fd678dda203338fca779112d","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":2,"emitted_findings":3,"total":5},"evidence_id":"47fa5bac-95a5-4350-8d7f-39b416f8f055","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"8d4d9efe41a81864140538f233916a754aef419215529889b37b187a35a0c7ad","status":"valid"},"source_digest":"6456a588659b53eb68f7898540d32f69e45a845a464702dbf022c9c1957b7732","version":1},"findings":[{"category":"traceability","check_key":"rebind-verification-branch-parity","description":"T1 says absent-row rebind does not invalidate, while C1, §1.2, and acceptance 4.2.1 require an absent-row rebind with different recorded-root active state to clear that machine/project's active project and file states. The verification contract therefore contradicts the implementation contract.","finding_id":"PCID-R10-F57","fix":"Revise the T1 rebind bullet to state that same-root and absent-row rebind with no state or matching-root state do not invalidate, while absent-row rebind with mismatched recorded-root state clears only that machine/project's active project and file states.","location":"T1 verification / P1 §1.2 / P4 §4.2","prevention":"Compare each verification bullet against the complete branch matrix in its governing deliverable and acceptance items.","principle":"Verification summaries must preserve every acceptance branch whose outcomes differ.","root_cause":"T1 collapses both absent-row rebind branches into a no-invalidation statement even though sections 1.2 and 4.2 require mismatched recorded-root state to be cleared.","section_id":"4.2","severity":"blocking"},{"category":"traceability","check_key":"final-privilege-manifest-parity-consumer","description":"Section 6.1 removes projects.repo_path from managed_postgres_privileges.json but omits tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set. The final leaf can leave the intermediate id/name/deleted_at/repo_path expectation failing or require an undeclared test edit.","finding_id":"PCID-R10-F58","fix":"Add tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set to §6.1 Targets and acceptance, updating final projects-column parity to id, name, and deleted_at after repo_path is removed.","location":"P6 §6.1","prevention":"For every repeated registry or generated artifact target, map its exact-set parity tests into every leaf that changes the staged shape.","principle":"Each staged mutation of a shared exact-set registry must own its parity assertion at that stage.","root_cause":"Section 1.1 owns the privilege-parity test for the intermediate projects column set, while section 6.1 mutates the same manifest to remove repo_path without targeting or accepting the parity test.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"cutover-multiple-authoritative-checkouts","description":"A project may already have valid checkout rows on two machines, but §6.1 defines covered preflight only for exactly one verified existing checkout. The plan does not say whether multiple authoritative rows are preserved, treated as covered, or rejected, so a valid multi-machine project can abort or be reduced incorrectly.","finding_id":"PCID-R10-F59","fix":"Define one or more verified existing checkout rows as authoritative coverage, preserve every row, and count only candidate machine/project pairs lacking a row as unresolved. Add a cutover test with two valid existing machine rows that performs zero inserts and preserves both rows and both machines' index state.","location":"P6 §6.1 / P5 §5.2","prevention":"Exercise cutover preflight with zero, one, and multiple valid existing machine checkout rows before finalizing cardinality rules.","principle":"Migration preflight must define preservation and insertion outcomes for every valid source-state cardinality allowed by the target model.","root_cause":"The cutover's covered branch is written for exactly one verified existing checkout even though the model and §5.2 allow one project to have authoritative checkout rows on multiple machines.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"3dade4fb-b105-4e0a-8682-495bfe454655","round":10,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

**Handoff** `kind: verification`

- reason: review cap reached after Round 10 `needs_review`; user-requested Rounds 11-18 also `needs_review`; Round 18 findings are only prose-of-prior-repairs
- action: coordinator-derived M1 via `derive_plan_handoff_manifest` / `apply_plan_handoff_manifest`; M1 covers labels updated for 1.3.8, 2.1.13, 2.1.14, 2.1.15, 2.2.3, 2.2.4, 2.2.5, 2.3.4, 3.2.9, 3.3.7, 3.3.8, 4.2.9, and 6.1.14
- completed_plan_review_rounds: 18
- do_not_build: true

Round 11 needs_review: 5 blocking findings. All accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. User-requested extra round after the R10 cap.

- PCID-R11-F60 accepted: Constraints prep now registers the plan and generates/validates the coverage ledger before build or expansion.
- PCID-R11-F61 accepted: 2.2 now requires C1 checkout-domain errors to propagate through ensure_project_in_db / resolve / resolve_hook_project_context; skipped=True is not a refusal.
- PCID-R11-F62 accepted: 3.3 owns TestGetCommitCount; commit-count uses session.machine_id plus the checkout resolver.
- PCID-R11-F63 accepted: 6.1 owns the production-Python DDL inventory counts for project_checkout_cutover.py.
- PCID-R11-F64 accepted: 4.2 owns Python file-state upsert/delete lock and primary-versus-overlay mode.

```json plan-review-round
{"evidence_id":"a6bfd203-c9c0-4afa-84cb-4f7d6edf6743","plan_hash":"12347a02062d9655179243bc289e77484b0e0552d985a93fa0cb39594d68d16f","round_number":11,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"0ad42c120a9e9be2554131cf364b4d67f895f34a5b8868ba42497084a6be9b68","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":7,"emitted_findings":5,"total":12},"evidence_id":"a6bfd203-c9c0-4afa-84cb-4f7d6edf6743","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":5,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":6,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"d0b3557e81807b2e8886946e5cdc11bbdf6f8fae119fdfeb809a92e5ddf6fae3","status":"valid"},"source_digest":"7ca2a593f76a41a24319191238d25a4181ad406152a8196afff98779a40116bc","version":1},"findings":[{"category":"missing-requirement","check_key":"plan-registry-ledger-preflight","description":"The canonical artifact has a valid Plan ID and M1, but read-only lookups by project-checkout-identity and #19651 both return plan_not_found, and .gobby/plans/project-checkout-identity.coverage-ledger.yaml is absent. The plan-storage contract makes the plans table authoritative, while expansion QA requires the companion ledger, so the stated preparation is incomplete.","finding_id":"PCID-R11-F60","fix":"Extend the Constraints preparation prerequisite: after promoting and rewiring #19651, register .gobby/plans/project-checkout-identity.md as active plan_id project-checkout-identity with root_task_ref #19651; generate .gobby/plans/project-checkout-identity.coverage-ledger.yaml; verify both registry lookups resolve to the same row and the ledger validates before gobby build or expansion.","location":"Constraints pre-build preparation / P1 section 1.1","prevention":"Before approving any new epic implementation plan, verify get_plan by canonical Plan ID and root task resolves to one active row and verify the required .coverage-ledger.yaml companion exists.","principle":"Every new epic implementation plan must be registered under its canonical Plan ID and root task and must have its required bootstrap coverage ledger before build or expansion.","root_cause":"The preparation checklist exhaustively names task-graph and validation mutations but omits the authoritative plan-registry row and managed coverage-ledger companion.","section_id":"1.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"hook-registration-refusal-propagation","description":"Section 2.2 requires typed non-restoring refusals for soft-deleted or otherwise invalid checkout registration, but the current hook resolver swallows the common bases used by those errors. Hook resolution can therefore continue with the marker project ID after registration was refused.","finding_id":"PCID-R11-F61","fix":"Specify that checkout-domain refusals propagate through resolve_hook_project_context or produce a structured refused result that stops downstream hook/session processing. Keep logging-only behavior solely for explicitly non-fatal failures, and add tests asserting no successful HookProjectResolution or event/session continuation after each typed refusal.","location":"P2 section 2.2 / ProjectIdResolver.ensure_project_in_db","prevention":"For every new typed domain error, inspect broad catch sites and test the caller-visible outcome through the outer ingress boundary.","principle":"A typed ingress refusal must reach the ingress boundary and prevent downstream processing from treating the refused context as accepted.","root_cause":"ProjectIdResolver.ensure_project_in_db currently catches ValueError and RuntimeError, logs, and returns; ProjectIdResolver.resolve then returns the marker project ID regardless.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"session-route-checkout-test-seam","description":"src/gobby/servers/routes/sessions/core.py::_get_commit_count is explicitly cut over, while tests/servers/routes/test_sessions_routes.py::TestGetCommitCount remains outside all Targets and encodes the removed projects.repo_path query and cwd behavior. The leaf can leave its direct test seam broken or silently untested.","finding_id":"PCID-R11-F62","fix":"Add tests/servers/routes/test_sessions_routes.py to section 3.3 Targets and acceptance. Replace repo_path-row fakes with session.machine_id plus checkout resolver fakes, and cover local checkout success, missing checkout, missing machine, and foreign-machine refusal before git runs.","location":"P3 section 3.3 / sessions route commit-count helper","prevention":"For every targeted root-resolution function, use gcode to map its defining test class and include that test file in the owning deliverable.","principle":"A directly changed filesystem consumer must migrate its exact focused tests and fakes to the new identity contract.","root_cause":"Section 3.3 targets sessions/core.py but names only CLI session tests; the route-level TestGetCommitCount suite still fakes SELECT repo_path and asserts it becomes git cwd.","section_id":"3.3","severity":"blocking"},{"category":"traceability","check_key":"python-ddl-inventory-parity","description":"tests/storage/test_schema_contract.py::test_production_python_has_no_persistent_postgres_ddl compares every production Python DDL string against an exact Counter. The planned cutover module will add CREATE/ALTER/DROP operations and fail that invariant unless the test inventory is updated, but the file is absent from section 6.1.","finding_id":"PCID-R11-F63","fix":"Add tests/storage/test_schema_contract.py to section 6.1 Targets and acceptance, update _KEPT_ADJACENT_SQL with the exact post-implementation operation counts for src/gobby/storage/project_checkout_cutover.py, and retain exact equality so unexpected DDL still fails.","location":"P6 section 6.1 / production-Python schema contract","prevention":"Whenever a deliverable adds CREATE, ALTER, DROP, or REINDEX SQL to production Python, target the exact DDL inventory and pin operation counts.","principle":"Every intentional production-Python DDL site must update and run the repository's exact DDL inventory in the same deliverable.","root_cause":"The new project_checkout_cutover.py necessarily contains campaign bootstrap and transactional DDL, while _KEPT_ADJACENT_SQL currently enumerates only the account-identity cutover and section 6.1 omits its parity test.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"python-file-state-writer-lock-parity","description":"src/gobby/code_index/_storage/files.py can write or delete code_indexed_file_states without holding the checkout row FOR SHARE or verifying committed-root equality. Its concrete callers include test files already named in 4.2, so stale primary file state can bypass the rebind fence and the Python seam can diverge from the Rust contract.","finding_id":"PCID-R11-F64","fix":"Add src/gobby/code_index/_storage/files.py to section 4.2. Give upsert_file and delete_file an explicit primary-versus-overlay contract with authoritative root input; primary mode must lock the matching checkout row FOR SHARE through the write and require root equality. Migrate every Python caller and add paused upsert/delete versus rebind tests.","location":"P4 section 4.2 / Python code-index file storage","prevention":"For each active-state table, sweep every INSERT, UPSERT, UPDATE, and DELETE implementation plus direct callers before finalizing the lock boundary.","principle":"Every primary active-state writer, across all implementations and reference seams, must participate in the same checkout-lock and committed-root invariant.","root_cause":"Section 4.2 covers Rust file-state writers and the Python project-state writer but omits CodeIndexFileStorageMixin.upsert_file and delete_file, which mutate active file state without root or primary-versus-overlay mode.","section_id":"4.2","severity":"blocking"}],"reviewer_session":"943c3ffd-af1a-4dce-b4d2-9548663a8128","round":11,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 12 needs_review: 5 blocking findings. All accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. First of eight user-requested extra rounds after R11.

- PCID-R12-F65 accepted: 3.3 dirty-file resolution is overlay-aware via resolve_operation_root; registered overlay is inspected even without a primary checkout.
- PCID-R12-F66 accepted: 6.1 no_candidate_machine abort when a non-sentinel legacy-path project has empty checkout and candidate-machine sets.
- PCID-R12-F67 accepted: 2.1 NameAttachRejectedError failpoint after uniqueness rollback and before unlink.
- PCID-R12-F68 accepted: 6.1 bootstrap is CREATE TABLE IF NOT EXISTS without receipt refresh.
- PCID-R12-F69 accepted: tests/integration/test_edit_history.py leftover positional create setup plus residue positional-root query.

```json plan-review-round
{"evidence_id":"0c187398-9a6e-4c5e-8fdc-6fff6da4884b","plan_hash":"ce08b642f025efbf49834d46b7efcef41a821bae195559c7245b02852cc61647","round_number":12,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"34bcd1ab45aae351b7193a6f341cb04a7f276248377fb7e26e061adc8fd1fff6","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":5,"total":6},"evidence_id":"0c187398-9a6e-4c5e-8fdc-6fff6da4884b","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"3d927ea4c0db430f6c712316f77fdb2a608c8956005f92d7b4baf03900dc9b54","status":"valid"},"source_digest":"5e0b495c3f016aec4d08645d8fab1ee5958bec818b2b796db76c0e971801f53e","version":1},"findings":[{"category":"unhandled-edge","check_key":"workflow-overlay-root-precedence","description":"WorkflowHookHandler._resolve_project_path currently preserves an isolated session's event or metadata root. The planned require_root cutover would evaluate dirty-file rules against the primary checkout, and the named workflow tests cover only local-checkout success and missing checkout.","finding_id":"PCID-R12-F65","fix":"Change § 3.3 to pass event cwd or metadata.project_path as overlay_path to resolve_operation_root, preserve a registered local worktree or clone even when no primary checkout exists, propagate typed refusals for invalid or foreign overlays, and add a dirty-file test proving the registered overlay is inspected.","location":"P3 / § 3.3","prevention":"For every filesystem consumer, classify primary-only versus overlay-aware behavior and test primary checkout, registered overlay, invalid overlay, foreign-machine overlay, and missing-checkout cases.","principle":"Overlay-aware consumers must validate and preserve an explicit operation root before primary-checkout fallback.","root_cause":"Section 3.3 classifies workflow dirty-file resolution as primary-only require_root even though the live handler intentionally prioritizes event cwd and metadata.project_path for isolated sessions.","section_id":"3.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"cutover-empty-candidate-set","description":"LocalProjectManager can persist a non-empty projects.repo_path independently of any machine-owned evidence. Section 6.1 has no project-level outcome for that state, allowing the campaign to drop the only path without creating a checkout.","finding_id":"PCID-R12-F66","fix":"Add a no_candidate_machine preflight abort for each non-sentinel legacy-path project whose authoritative checkout set and candidate-machine set are both empty. Retain repo_path, include the case in rehearsal evidence and operator rebind guidance, and cover abort-then-rebind-then-rerun in 6.1.1.","location":"P6 / § 6.1","prevention":"Include zero-candidate-machine projects in migration matrices and prove every non-sentinel legacy path reaches preserved, inserted, excluded, or abort status.","principle":"Cutover preflight must classify every legacy identity-bearing row, including an empty machine-evidence domain.","root_cause":"The preflight creates unresolved work only from machine-derived candidate pairs, so a legacy project with repo_path, zero checkout rows, and zero session/index/worktree/clone evidence produces no pair and can pass coverage vacuously.","section_id":"6.1","severity":"blocking"},{"category":"weak-testability","check_key":"name-collision-pre-unlink-recovery","description":"The plan promises crash-retry cleanup for a losing same-name marker, yet it cannot deterministically exercise a crash after uniqueness rollback and before unlink. That state leaves the orphan marker the retry logic is required to recognize.","finding_id":"PCID-R12-F67","fix":"Add a NameAttachRejectedError failpoint immediately after database rollback and before marker unlink. Extend 2.1.6 or 2.1.8 to prove retry removes only the still-matching losing marker and preserves any later replacement marker.","location":"P2 / § 2.1","prevention":"For each post-publication typed failure, enumerate and test rollback, pre-unlink, post-unlink, and post-directory-fsync recovery states.","principle":"Every durable-marker cleanup branch needs a deterministic seam after transaction rollback and before marker unlink.","root_cause":"The distinct-root NameAttachRejectedError branch begins its failpoints after unlink, while its adjacent checkout-root-taken and overlay-rejection branches pin the post-rollback/pre-unlink crash state.","section_id":"2.1","severity":"blocking"},{"category":"missing-requirement","check_key":"campaign-bootstrap-create-if-not-exists","description":"A plain CREATE TABLE implementation satisfies the current wording while violating the settled rerunnable-bootstrap contract on an already-created predecessor table.","finding_id":"PCID-R12-F68","fix":"State that predecessor bootstrap executes CREATE TABLE IF NOT EXISTS project_checkouts, then verifies the complete table, constraint, policy, and grant shape without refreshing the receipt. Pin that exact primitive in 6.1.4 and the exact DDL inventory.","location":"P6 / § 6.1","prevention":"Translate every settled migration primitive into both deliverable prose and a focused acceptance assertion.","principle":"Rerunnable campaign bootstrap DDL must pin its idempotent primitive in implementation and acceptance.","root_cause":"The settled CREATE TABLE IF NOT EXISTS requirement is absent from § 6.1 and 6.1.4, which currently say only that bootstrap creates and verifies project_checkouts.","section_id":"6.1","severity":"blocking"},{"category":"traceability","check_key":"positional-project-create-fixture","description":"tests/integration/test_edit_history.py passes a newly created directory as the second positional root argument without a verified machine, marker, or checkout. It is absent from every Target and can evade a keyword-only repo_path residue query.","finding_id":"PCID-R12-F69","fix":"Add tests/integration/test_edit_history.py to the owning § 6.1 leftover-fixture Targets, migrate its setup to a verified machine, marker, project, and checkout, and make the residue query cover positional ordinary-root arguments.","location":"P6 / § 6.1","prevention":"Sweep constructor usages by symbol and argument position, then map each confirmed ordinary-root caller to exactly one owning deliverable and test setup.","principle":"Every caller affected by an identity-constructor contract change needs an explicit owning target, including positional call shapes.","root_cause":"The named leftover inventory and repo_path-oriented residue sweep omit tests/integration/test_edit_history.py, whose positional LocalProjectManager.create root reaches the new machine/marker/checkout validation path.","section_id":"6.1","severity":"blocking"}],"reviewer_session":"ffac4502-d5d6-4eac-b418-082239034e12","round":12,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 13 needs_review: 3 blocking findings. All accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. Not fixer-prose of R1–R12. Second of eight user-requested extra rounds after R11.

- PCID-R13-F70 accepted: C1 shared mutation mapping includes MissingMachineContextError as HTTP 409 for register and rebind; 2.4.3 covers it.
- PCID-R13-F71 accepted: require_root and resolve_operation_root call require_local_machine_id after the missing-machine check and before lookup; 1.3.8 covers foreign-session refusal.
- PCID-R13-F72 accepted: resolve_tool_session is a LEFT JOIN with nullable root_path; issue_tool_request classifies overlay-only sessions; 3.2.9 covers it.

```json plan-review-round
{"evidence_id":"29b3c2aa-69ec-45f6-a67b-8cdf9e93d9b8","plan_hash":"0057f95ac52012ad1b13feabbd16d92912148868f20ee3aed8a41f4384452f31","round_number":13,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"bf3d681c77f42d390097096cbc731cc4e62f303248492cdf18fc16938c144d94","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":3,"total":3},"evidence_id":"29b3c2aa-69ec-45f6-a67b-8cdf9e93d9b8","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":0,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"1aba107bb7caa938a422beee78868c0c8ec8ca0521e201e9017ed62bfb10b852","status":"valid"},"source_digest":"e6d650843a9fbe6af1301629ca9f134bfca6465ac62bdd6574ed1aed9e3fba2b","version":1},"findings":[{"category":"unhandled-edge","check_key":"http-missing-machine-error-mapping","description":"Register and rebind can raise MissingMachineContextError before filesystem access, yet C1 maps missing project, foreign machine, checkout conflicts, validation failures, and sentinels without assigning this reachable error a response. Section 2.4.3 inherits that incomplete mapping, leaving implementations free to leak a 500 or choose inconsistent statuses.","finding_id":"F70","fix":"Add MissingMachineContextError to C1's shared mutation mapping as HTTP 409, state that both register and rebind return it when local machine identity is unavailable, and add explicit cases to 2.4.3 plus the M1 criterion.","location":"C1 HTTP contract / § 2.4","prevention":"For each public mutation, enumerate every typed exception from preconditions, validation, and persistence; map and test each outcome.","principle":"Every typed failure reachable from a public HTTP operation needs an explicit status mapping and acceptance case.","root_cause":"Checkout register and rebind call require_local_machine_id before mutation, while the shared HTTP mapping omits MissingMachineContextError.","section_id":"2.4","severity":"blocking"},{"category":"unhandled-edge","check_key":"foreign-machine-filesystem-resolution","description":"Task-tool callers pass session.machine_id into the shared root resolvers. A foreign session can therefore select another machine's opaque checkout string unless every caller adds an unstated guard, violating the plan's foreign-machine-before-filesystem invariant.","finding_id":"F71","fix":"Make require_root and resolve_operation_root call require_local_machine_id(machine_id, resource_kind=\"project_checkout\", resource_id=project_id) before checkout or overlay lookup. Add resolver and task-path tests proving foreign-session refusal before filesystem access; retain LocalProjectCheckoutManager.get/list_for_machine for opaque inspection.","location":"§ 1.3 / § 3.2","prevention":"At every opaque-path-to-filesystem boundary, verify the supplied machine against the local daemon before checkout or overlay lookup; keep cross-machine inspection on filesystem-free manager methods.","principle":"A daemon filesystem resolver must prove machine ownership before returning or touching an opaque machine-owned path.","root_cause":"require_root and resolve_operation_root accept machine_id directly, while local-machine verification is specified for writes and selected callers instead of the shared filesystem-resolution boundary.","section_id":"1.3","severity":"blocking"},{"category":"bad-sequencing","check_key":"overlay-only-session-auth-resolution","description":"The plan allows a registered local worktree or clone to be an operation root when no primary checkout exists, and 3.2.4 requires issue_tool_request to accept that case. The checkout-only inner join returns no session row without a primary checkout, so overlay authorization is never reached.","finding_id":"F72","fix":"Specify a LEFT JOIN from each eligible session to project_checkouts in both the fresh 3.2 definition and live 6.1 recreation, permit nullable root_path, and have issue_tool_request resolve the requested path through resolve_operation_root after verifying session project and machine. Add an overlay-only session to managed-credential and live/fresh SQL tests while preserving the expired/deleted filter.","location":"§ 1.3 / § 3.2 / § 6.1","prevention":"Cross-check upstream SQL eligibility against downstream primary, overlay-only, missing, foreign, and expired session states whenever authorization feeds path resolution.","principle":"An authorization precondition must preserve every operation-root state that the downstream resolver explicitly supports.","root_cause":"resolve_tool_session uses an inner project_checkouts join before issue_tool_request performs overlay-aware authorization, eliminating overlay-only sessions before their registered overlay can be validated.","section_id":"3.2","severity":"blocking"}],"reviewer_session":"9bc6ca16-350d-478f-be59-d51b4456a5c5","round":13,"round_number":13,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 14 needs_review: 2 blocking findings. Both accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. Not fixer-prose of R1–R13. Third of eight user-requested extra rounds after R11.

- F73 accepted: rename_project is a 2.3 Target; rename commits projects.name without repo_path and refreshes only the calling-daemon marker; existing marker ID keeps the database name (2.1.14 / 2.3.4).
- F74 accepted: every post-publication OverlayRegistrationRejectedError, including validate_checkout_root after marker install, unlinks the still-matching marker; 2.1.13 covers the pre-validation race and failpoints.

```json plan-review-round
{"evidence_id":"350b4ce9-ac10-4b88-94ae-ee52874d25a3","plan_hash":"5cbcbf4155275461879f4f8e210887e118d6ff2e27b394454944f939d3483f9d","round_number":14,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"ae232fcd61ce46d05c891cc7a32c4fad57bb6e5252dcd3a4180e9430abf12c65","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":2,"total":6},"evidence_id":"350b4ce9-ac10-4b88-94ae-ee52874d25a3","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"8ca6903419a1c035ddafec0f6ce9203075ad0ca5c35a51d9d95003e7abdf12ca","status":"valid"},"source_digest":"c013a6ae5645714eb0c5221750e7f6c3bdf949022efb62dea3cd83e067d55c01","version":1},"findings":[{"category":"missing-requirement","check_key":"removed-field-consumer-and-replica-metadata","description":"Section 1.3 removes Project.repo_path, yet src/gobby/cli/projects.py::rename_project still dereferences it after committing the global name change. A local-only marker rewrite also leaves other machine markers stale, while § 2.1 does not say that an existing marker ID with an old name must preserve the database name; the current ensure_exists implementation would let that stale marker revert the rename.","finding_id":"F73","fix":"Add src/gobby/cli/projects.py::rename_project to § 2.3 Targets and add a dedicated acceptance item. Make the database name authoritative for an existing marker ID. Logical rename succeeds with no local checkout; when a calling-daemon checkout exists, refresh only that marker as best-effort metadata after the database commit and warn on refresh failure. On later ID-targeted init or hook ingress, keep the database name and refresh the stale local marker instead of updating projects.name. Test checkout-present, checkout-null, post-commit marker-write failure, and rename-on-machine-A followed by init on stale machine-B.","location":"P2 / § 2.3, interacting with § 2.1 and § 1.3","prevention":"Before removing a dataclass field, run a gcode usage sweep, map every consumer to an exact Target, and test global mutations against stale metadata in another machine checkout.","principle":"A removed model field requires migration of every direct consumer and an explicit convergence rule for machine-local metadata after a global mutation.","root_cause":"The CLI target list covers list, show, update, repair, and refresh paths but omits rename_project; the existing ID-targeted ensure_exists path also lets a marker name overwrite the database name.","section_id":"2.3","severity":"blocking"},{"category":"unhandled-edge","check_key":"post-publication-typed-refusal-cleanup","description":"No-marker init publishes the marker before validation can confirm the checkout write. If an overlay row appears after publication and validate_checkout_root detects it, the current plan never reaches the narrowly specified manager-recheck cleanup branch. The durable marker then points at no project row, and retry refuses the overlay again.","finding_id":"F74","fix":"Broaden § 2.1 so every post-publication OverlayRegistrationRejectedError, whether raised by validate_checkout_root or by register's transactional recheck, confirms the UUID has no project row, unlinks only the still-matching marker, fsyncs the directory, and re-raises. Add the race where the overlay appears after marker install but before validation, with failpoints before unlink, after unlink, and after directory fsync.","location":"P2 / § 2.1","prevention":"Enumerate every exception edge between marker install and project-plus-checkout commit, and route each post-publication refusal through the matching-marker recovery state machine.","principle":"Every failure after durable marker publication must either establish the matching database identity or durably remove only that still-matching marker.","root_cause":"The cleanup branch is conditioned on OverlayRegistrationRejectedError from the manager's same-transaction recheck, although validate_checkout_root can raise the same refusal after marker publication and before that transaction begins.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"b7f3314f-3254-40b2-8378-5bad0f98ad5e","round":14,"round_number":14,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 15 needs_review: 2 blocking findings. Both accepted scoped. No wiki-home, USER.md, Tailnet, tenancy, dual-read, overlay-requires-checkout, or advisory-lock findings. Not fixer-prose of R1–R14. Fourth of eight user-requested extra rounds after R11.

- F75 accepted: tests/e2e/test_worktrees_e2e.py leftover git_repo_with_origin repo_path fixture plus residue ownership in 6.1; keep single 6.1.3.
- F76 accepted: expected-id crash-durable marker refresh helper for init, ensure_exists, hook ingress, and rename; 2.1.15 covers replacement races and failpoints.

```json plan-review-round
{"evidence_id":"cb0b00cd-bf1f-4c97-8df2-e8cf928bac13","plan_hash":"b85a48ce4ebb76eb975ef42da555565fa0ead360884ed370a909e0143110521c","round_number":15,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"c3574db4da131b073968210cd04d988ac9a5261e03b6ec6a252f4e1e60b9dbe2","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":4,"emitted_findings":2,"total":6},"evidence_id":"cb0b00cd-bf1f-4c97-8df2-e8cf928bac13","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":2,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":4,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"29827ee3deddf619288303eab5b7430963387d773f10710af5e06324f654ca00","status":"valid"},"source_digest":"2931cd6176dfc2972831eaefae98392fe014fa3218dda0fa8a8958fa888fbc55","version":1},"findings":[{"category":"traceability","check_key":"e2e-marker-fixture-target-parity","description":"tests/e2e/test_worktrees_e2e.py::git_repo_with_origin still writes repo_path into .gobby/project.json and uses repo_path-shaped registration, but the file is absent from every exact Target. The post-cutover residue contract will reject this fixture.","finding_id":"PCID-R15-F75","fix":"Add tests/e2e/test_worktrees_e2e.py to § 6.1 Targets and migrate the fixture to the stable marker schema without repo_path plus verified machine/project/checkout registration. Keep the existing single § 6.1.3 residue acceptance; do not add a leaf.","location":"§ 6.1 — Run the project-checkout-cutover campaign","prevention":"Sweep project.json constructors for repo_path and reconcile every known hit with exact Targets before approval.","principle":"Every known file whose marker fixture violates the new identity schema must be owned by an exact Target; residue sweeps supplement target ownership.","root_cause":"A manually authored worktree E2E marker fixture escaped the named leftover-fixture inventory.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"post-publication-marker-refresh-cas-durability","description":"Section 2.1's still-matching language and § 2.3's best-effort refresh do not define an atomic expected-ID compare-and-install contract or durability for later rewrites. The current helper reads and then calls os.replace without fsync; a concurrent marker replacement can receive another project's name, and a crash after the database rename commits can lose the refreshed marker.","finding_id":"PCID-R15-F76","fix":"Add one expected_project_id-guarded marker-refresh contract used by ID-targeted init, ensure_exists, hook ingress, and rename. It must preserve the payload, refuse an ID change, use a tested conditional install that cannot overwrite a replacement, fsync the completed temporary file, durably install it, and fsync the parent directory. Cover replacement races and failpoints; rename refresh failure still warns while the committed database name remains authoritative.","location":"§§ 2.1 and 2.3 — marker-authoritative init and rename","prevention":"For every marker writer, test expected-ID replacement races plus failpoints after temporary-file fsync, installation, and directory fsync.","principle":"Every post-publication marker rewrite must be conditional on the expected project UUID and crash-durable.","root_cause":"The plan specifies identity guards and durability for initial marker publication but treats later name-refresh paths as best-effort overwrites.","section_id":"2.1","severity":"blocking"}],"reviewer_session":"1bb90762-e686-4029-8ba6-864a96770150","round":15,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 16 needs_review: 4 blocking findings. F77 accepted scoped. F78 declined as suite-wide leftover inventory already gated by 1.3 migrate-on-owning-leaf plus 6.1.3 residue. F79 declined as F12/F76 lock expansion; flock remains the refresh install window. F80 declined as F12 shared-serialization / overlay-mutation scope creep; checkout-side same-txn overlay recheck stays. Fifth of eight user-requested extra rounds after R11.

- F77 accepted: hook stale-name refresh needs 2.2.4 plus M1 covers.
- F78 declined: complete caller inventory / nine extra leftover Targets; residue contract and 1.3 owning-leaf migration already cover create(..., repo_path=) leftovers.
- F79 declined: sibling coordination lock for all marker writers; F76 already specifies expected-id refresh and replacement refusal without a new lock subsystem.
- F80 declined: machine-row FOR UPDATE plus worktrees/clones as 1.2 Targets; same-txn overlay recheck is the settled boundary.

```json plan-review-round
{"evidence_id":"9d5a6c13-9e1b-419e-8f44-53b2f0924331","plan_hash":"f9dbd8c00527c4f893a2e109e3c8d583972465a24f5f240244a8327871e75374","round_number":16,"round_result":{"coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f9e2ae473da3530b7d6935210ae14d0aad12faee033971b3f39c46a21579c457","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":4,"total":4},"evidence_id":"9d5a6c13-9e1b-419e-8f44-53b2f0924331","lanes":[{"candidate_count":1,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":2,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"f7f605834a2e68261347756f83af09dfc6f8d304ae809019b9307cfe436577a2","status":"valid"},"source_digest":"8e6418fbe3addc98e7be6ae1c539b47330af4f6a0e0be59d14783bd1687a296b","version":1},"findings":[{"category":"weak-testability","check_key":"hook-stale-marker-acceptance","description":"Hook ingress must preserve the authoritative projects.name value and refresh only a still-matching local marker through the expected-ID helper, yet no acceptance item proves that hook-level integration.","finding_id":"PCID-R16-F77","fix":"Add acceptance item 2.2.4 targeting tests/hooks/test_hook_manager.py. Require a stale-name hook request to leave projects.name unchanged, refresh the marker through the expected-ID protocol, and preserve a concurrently replaced marker; include the criterion in M1.","location":"P2 / § 2.2","prevention":"For every named helper consumer, add a caller-level acceptance item and derived manifest criterion covering successful behavior plus concurrency and refusal paths.","principle":"Every required integration branch needs manifest-backed acceptance evidence.","root_cause":"The stale-name hook behavior is required in the section prose, while section 2.2 acceptance and its derived manifest criteria cover registration exclusions, soft-deleted refusal, and typed-error propagation only.","section_id":"2.2","severity":"blocking"},{"category":"traceability","check_key":"project-root-caller-target-parity","description":"Direct root-bearing setup remains outside declared Targets in tests/e2e/test_linear_setup_e2e.py, tests/integration/test_worktree_merge_integration.py, tests/mcp_proxy/tools/test_worktrees_session_resolution.py, tests/servers/routes/admin/test_stats.py, tests/servers/routes/test_agent_spawn_routes.py, tests/servers/routes/test_agents_routes.py, tests/storage/sessions/test_lifecycle.py, tests/workflows/expansion_qa_helpers.py, and tests/tasks/test_diff_paging.py.","finding_id":"PCID-R16-F78","fix":"Add those files as exact section 6.1 Targets. Convert FK-only setup to path-free project creation, filesystem-bearing setup and fakes to the verified machine/marker/project/checkout helper, and publish the matching marker before checkout registration in test_linear_setup_e2e.py. Make the complete indexed caller inventory part of the residue scope.","location":"P6 / § 6.1 interacting with P1 / § 1.3","prevention":"Resolve LocalProjectManager create, ensure, and update usage by symbol, then reconcile every filesystem-bearing caller and fake with one exact Target before accepting residue closure.","principle":"Every caller that must change under an API or identity-contract cutover needs ownership through an exact Target.","root_cause":"The residue sweep describes the prohibited caller shape, while the target inventory omits verified root-bearing LocalProjectManager callers and project-manager fakes that must change under sections 1.3 and 6.1.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"marker-pathname-cas","description":"The proposed expected-ID refresh and still-matching cleanup are vulnerable to pathname replacement races, so a concurrent marker can be clobbered, unlinked, or overwritten by a stale adjacent writer.","finding_id":"PCID-R16-F79","fix":"Require one stable sibling coordination lock for initial publication, expected-ID refresh, verification and field updates, and still-matching cleanup. Under that lock, reread the current pathname, verify the expected ID immediately before install or unlink, fsync the directory, and route hook, rename, and Linear writers through the protocol. Add deterministic replacement-before-install and replacement-before-unlink races.","location":"P2 / §§ 2.1–2.3 and P3 / § 3.3","prevention":"Enumerate every marker writer and remover, then test pathname replacement between open or comparison and final install or unlink.","principle":"A marker compare-and-mutate protocol must serialize the stable directory entry rather than only a replaceable inode.","root_cause":"flock on the opened project.json inode does not prevent another writer from replacing its pathname; refresh or cleanup can then overwrite or unlink the replacement. Adjacent marker writers also perform read-then-replace sequences outside a shared stable lock.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"overlay-checkout-cross-table-serialization","description":"An overlay can commit after checkout validation or move onto an established checkout path, leaving the same machine and path represented simultaneously as a primary checkout and an overlay.","finding_id":"PCID-R16-F80","fix":"Expand section 1.2 Targets to src/gobby/storage/worktrees.py and src/gobby/storage/clones.py plus focused tests. Require checkout register or rebind and every overlay create, adopted registration, and path-changing update to acquire one shared database boundary, such as the machine row FOR UPDATE, then check all three path registries before mutation. Test both concurrent commit orders and sequential path updates.","location":"P1 / §§ 1.2–1.3 interacting with P2 / § 2.1","prevention":"Walk both commit orders and every path-changing update for each cross-table exclusion invariant, and require both sides to acquire the same serialization primitive before rechecking.","principle":"Mutually exclusive identities stored in separate tables require one shared serialization boundary enforced by both mutation families.","root_cause":"A checkout-side overlay recheck observes only its transaction snapshot, while worktree and clone create, adopted registration, and path-changing update perform separate-table mutations without a reverse checkout check or common lock; schema uniqueness is table-local.","section_id":"1.2","severity":"blocking"}],"reviewer_session":"b38d3f3a-a76e-4b6f-8657-ed54462f2cde","round":16,"round_number":16,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 17 needs_review: 4 blocking findings. F81 declined as F78 leftover-inventory treadmill; 1.3 owning-leaf migration plus 6.1.3 residue already cover create/ensure/update leftovers. F82 declined as F79/F12 lock expansion; flock remains the refresh install window. F83 declined as F80 shared-serialization / overlay-mutation creep; checkout-side same-txn overlay recheck stays. F84 declined as hook_manager/admission rearchitecture; registration stays on the cwd-marker ensure_project_in_db path. Added 2.2.5 so implementers do not expand register to every resolution source. Sixth of eight user-requested extra rounds after R11.

- F81 declined: complete manager-call inventory / 56 extra leftover Targets; residue contract and 1.3 owning-leaf migration already cover create(..., repo_path=) leftovers.
- F82 declined: sibling coordination lock for all marker writers; F76 already specifies expected-id refresh and replacement refusal without a new lock subsystem.
- F83 declined: machine-row FOR UPDATE plus worktrees/clones as 1.2 Targets; same-txn overlay recheck is the settled boundary.
- F84 declined: keep resolve read-only and register after terminal admission from hook_manager; Constraints keep hook_manager registration-free except the one-line delegate; cwd-path ensure_project_in_db remains the only register/refresh path. Added 2.2.5 and M1 covers.

```json plan-review-round
{"evidence_id":"d266ce43-7043-48cc-991c-140402413a53","plan_hash":"dd9252eddc865e57c86cf68bc282fa5b81065c4b8cdceb401153c9a18b5d9b23","round_number":17,"round_result":{"artifact_path":".gobby/plans/project-checkout-identity.md","coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"030f0c647a7240a6f73ffcfada25ecebf9a38aa39c36969603aa12d90d5c50ee","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":0,"emitted_findings":4,"total":4},"evidence_id":"d266ce43-7043-48cc-991c-140402413a53","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"791fe5cfda3716fb26822179f69fd3cae890da26dbffdc155a6cecae26dbd3f5","status":"valid"},"source_digest":"ad9a67651fb224e554fbaf70c0823ba8c61218263c8ab8da2158c7ca993687f1","version":1},"evidence_id":"d266ce43-7043-48cc-991c-140402413a53","findings":[{"category":"traceability","check_key":"project-root-caller-target-parity","description":"Section 6.1 still omits a large current caller inventory. The repository sweep found at least 56 untargeted test files containing 128 direct same-line repo_path manager calls plus a positional-root create call; representative omissions include tests/build/test_child_merge_repair.py, tests/build_pipeline/test_controls.py, and tests/storage/sessions/test_lifecycle.py. Acceptance 6.1.3 would detect residue after implementation while no leaf owns these required fixture edits.","finding_id":"PCID-R17-F81","fix":"Expand § 6.1 Targets to enumerate the complete current manager-call inventory, including multiline and positional variants. Convert filesystem-bearing fixtures to the verified machine/marker/project/checkout helper, remove root arguments from FK-only setup, and require 6.1.3 to prove that exact inventory is empty.","location":"P6 / § 6.1 interacting with P1 / § 1.3","prevention":"Resolve create, ensure_exists, and update callers with gcode; check same-line, multiline, positional, fake, and direct Project.repo_path variants; reconcile every hit against exact Targets before review.","principle":"Every caller that must change under an identity-contract or API cutover needs exact deliverable ownership; a residue assertion verifies completion but cannot own omitted edits.","root_cause":"The cutover leaf names a partial fixture inventory while its prohibited-residue contract spans every LocalProjectManager create, ensure, and update caller that still supplies filesystem-root identity.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"marker-pathname-cas","description":"The expected-ID helper and losing-marker cleanup remain vulnerable to pathname replacement. A concurrent writer can replace project.json after the plan's inode check or still-matching read, causing refresh to overwrite a later marker or cleanup to unlink it; hook, rename, verification, and Linear writers share the same unsafe boundary.","finding_id":"PCID-R17-F82","fix":"Revise § 2.1 to use one stable sibling lock file for initial publication, expected-ID refresh, verification and field updates, and every still-matching cleanup. Under that lock, reread project.json, verify expected ID immediately before install or unlink, perform the mutation, and fsync the directory. Route init, hook, rename, and Linear writers through it and add deterministic replacement-before-install and replacement-before-unlink tests.","location":"P2 / §§ 2.1–2.3 and P3 / § 3.3","prevention":"Enumerate initial publication, refresh, field update, and cleanup paths, then test pathname replacement immediately before install and immediately before unlink for every consumer.","principle":"A compare-and-mutate protocol for project.json must serialize the stable directory entry across every writer and remover.","root_cause":"The plan locks the currently opened marker inode for refresh and performs still-matching cleanup as separate compare and unlink steps; neither protects the pathname from replacement between verification and mutation.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"overlay-checkout-cross-table-serialization","description":"A machine-local root can still become both a primary checkout and a worktree or clone. Sequential overlay creation after checkout registration succeeds, and concurrent writers can both pass absence checks and commit because only the checkout side rechecks overlay membership.","finding_id":"PCID-R17-F83","fix":"Add exact § 1.2 Targets for LocalWorktreeManager and LocalCloneManager create, register_adopted, and path-changing update methods. Require checkout and overlay writers to lock the same machine row FOR UPDATE, then recheck project_checkouts, worktrees, clones, and isolation paths before mutation. Add sequential refusal tests and both concurrent commit orders.","location":"P1 / §§ 1.2–1.3 interacting with P2 / § 2.1 and P5 / § 5.2","prevention":"Walk checkout-then-overlay, overlay-then-checkout, both concurrent commit orders, adopted registration, and every path-changing update while verifying all writers acquire the same database boundary.","principle":"Mutually exclusive identities stored in separate tables require one shared serialization boundary enforced by both mutation families.","root_cause":"Checkout register and rebind inspect overlay tables, while worktree and clone create, adopted-registration, and path-changing update methods do not reciprocally inspect or serialize against project_checkouts; table-local uniqueness cannot enforce the cross-table invariant.","section_id":"1.2","severity":"blocking"},{"category":"bad-sequencing","check_key":"hook-registration-admission-order","description":"The hook plan both misses accepted registration paths and mutates too early. Explicit and session-based resolutions bypass ensure_project_in_db, while stale or ambiguous terminal hooks can reach project resolution and checkout registration before validate_managed_agent_hook rejects them.","finding_id":"PCID-R17-F84","fix":"Keep project resolution read-only. Add exact Targets for HookManager._handle_after_daemon_ready and the SESSION_START admission seam, then call one checkout-registration helper after the applicable identity/admission fence accepts and before session or event continuation. Cover cwd, explicit, session, existing-session, and pre-created-session sources; prove rejected terminal hooks make no checkout mutation and typed refusals block continuation.","location":"P2 / § 2.2","prevention":"Enumerate every resolver return path and every admission fence; verify mutation occurs once after acceptance and before continuation, and prove rejected paths leave storage unchanged.","principle":"Persistent checkout registration must follow successful ingress admission and must run for every accepted project-resolution source that carries a valid ordinary marker cwd.","root_cause":"The plan attaches registration to ProjectIdResolver.ensure_project_in_db inside project resolution. Explicit, session-derived, existing-session, contract-probe, and current-context branches return before that callback, while terminal resolution occurs before the managed-ingress identity fence.","section_id":"2.2","severity":"blocking"}],"reviewer_session":"b9f7d47b-ceb1-47c5-a99d-eb1abe6ede69","round":17,"round_number":17,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

Round 18 needs_review: 3 blocking findings. F85 declined as F81/F78 leftover-inventory treadmill; 1.3 owning-leaf migration plus 6.1.3 residue already cover create/ensure/update leftovers. F86 declined as F82/F79/F12 lock expansion; flock remains the refresh install window, not a shared lock for all marker writers. F87 declined as F83/F80 shared-serialization / overlay-mutation creep; checkout-side same-txn overlay recheck stays. No plan-body repair. Stop condition (c): findings are only prose-of-prior-repairs. Seventh of eight user-requested extra rounds after R11; R19 not launched.

- F85 declined: 74-module / 57-omission leftover Target inventory; residue contract and 1.3 owning-leaf migration already cover create(..., repo_path=) leftovers.
- F86 declined: exclusive_file_lock sidecar for publication, refresh, field updates, and cleanup; F76 already specifies expected-id refresh and replacement refusal without a new lock subsystem.
- F87 declined: worktrees/clones 1.2 Targets plus machine-row FOR UPDATE; same-txn overlay recheck stays.

```json plan-review-round
{"evidence_id":"d1caf084-6573-4169-9856-ef84f02ac345","plan_hash":"d93686259f092f6c338911c6f48a2fab38be5995fcf7a4c554fc162263f767a8","round_number":18,"round_result":{"artifact_path":".gobby/plans/project-checkout-identity.md","coverage_attestation":{"adjacent_variant_complete":true,"attestation_digest":"f0d6133ef7bd9ea0b3d2dfd8219dcf19e662e564c005b1932a9d40dca1f60efe","cross_lane_interaction_complete":true,"disposition_counts":{"dismissed":1,"emitted_findings":3,"total":4},"evidence_id":"d1caf084-6573-4169-9856-ef84f02ac345","lanes":[{"candidate_count":0,"lane_id":"requirements_traceability","status":"completed"},{"candidate_count":1,"lane_id":"repository_blast_radius","status":"completed"},{"candidate_count":3,"lane_id":"runtime_invariants","status":"completed"}],"shadow_manifest_status":{"entry_count":16,"manifest_digest":"aff1fe86bf40c54dbf93a787eddd6fe36ead8a18e341719c47c39b8663858857","status":"valid"},"source_digest":"a749db93997867b3e5a43dc9d5608fcefdffe6aa14e52d3f63c850cf8b78ea41","version":1},"findings":[{"category":"traceability","check_key":"project-root-caller-target-parity","description":"The indexed sweep found 74 affected test modules; 57 remain untargeted. Removing the repo_path parameter will either break those tests or force edits outside every leaf’s declared ownership, and the residue assertion detects that gap only after implementation.","finding_id":"PCID-R18-F85","fix":"Expand the owning sections’ Targets to enumerate the complete 74-module inventory, including the 57 omissions. Migrate filesystem-bearing fixtures to verified machine/marker/project/checkout setup, remove path arguments from FK-only and repo_path=None calls, and make acceptance 6.1.3 name the same exhaustive gcode query set.","location":"P6 / § 6.1 interacting with §§ 1.3 and 3.1–3.4","prevention":"Resolve all direct, multiline, positional, fake, and repo_path=None manager-call variants with gcode, then reconcile every hit against exact Targets before review.","principle":"Every caller that must change under an identity-contract or API cutover needs exact deliverable ownership.","root_cause":"The residue contract spans all LocalProjectManager create, ensure_exists, and update callers, while 57 verified test modules that still pass repo_path are absent from every deliverable Target.","section_id":"6.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"marker-pathname-cas","description":"The expected-ID refresh and still-matching cleanup cannot guarantee that a concurrent replacement marker remains untouched. An inode lock does not protect the replaceable project.json pathname, so stale refresh can overwrite a later marker and cleanup can unlink it.","finding_id":"PCID-R18-F86","fix":"Replace the inode-flock instruction with the repository’s existing stable sidecar `exclusive_file_lock(project_file)` protocol. Route initial publication, expected-ID refresh, field updates, and still-matching cleanup through that lock; reread and verify the pathname under the lock immediately before replace or unlink, fsync the directory, and add deterministic replacement-before-install and replacement-before-unlink tests.","location":"P2 / §§ 2.1–2.3","prevention":"Enumerate initial publication, refresh, field-update, and cleanup paths, then test replacement immediately before install and immediately before unlink.","principle":"A compare-and-mutate protocol for project.json must serialize the stable directory entry across every writer and remover.","root_cause":"The plan flocks the currently opened project.json inode, while concurrent writers can replace the pathname between the ID check and install or unlink.","section_id":"2.1","severity":"blocking"},{"category":"unhandled-edge","check_key":"overlay-checkout-cross-table-serialization","description":"A machine-local root can still become both a primary checkout and a worktree or clone. Sequential overlay creation after checkout registration and concurrent opposite-order writers can bypass the checkout-side recheck because uniqueness is table-local.","finding_id":"PCID-R18-F87","fix":"Add exact § 1.2 Targets for `LocalWorktreeManager` and `LocalCloneManager` create, `register_adopted`, and path-changing update methods plus focused tests. Require checkout and overlay writers to lock the same machine row `FOR UPDATE`, recheck project_checkouts, worktrees, and clones before mutation, and test sequential reverse creation plus both concurrent commit orders.","location":"P1 / §§ 1.2–1.3 interacting with §§ 2.1 and 5.2","prevention":"Walk checkout-then-overlay, overlay-then-checkout, both concurrent commit orders, adopted registration, and every path-changing update while verifying both sides acquire the same database boundary.","principle":"Mutually exclusive identities stored in separate tables require one shared serialization boundary enforced by both mutation families.","root_cause":"Checkout register and rebind recheck overlay tables, while worktree and clone create, adopted registration, and path-changing update do not reciprocally check project_checkouts or acquire a common lock.","section_id":"1.2","severity":"blocking"}],"reviewer_session":"3559ed07-8de0-4556-9356-ceae6de90b33","round":18,"round_number":18,"verdict":"needs_review"},"session_id":"d36d63f0-fa7b-4a7a-8ccd-b87dfddd80ea"}
```

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add project_checkouts to baseline 375
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '1.1.1: Baseline 375 creates `project_checkouts` with the stated
    columns, timestamp defaults, PK, unique root, cascading FKs, and forced RLS. file:
    `crates/gcore/assets/schema/baseline.sql`.

    1.1.2: Machine-scoped grants list `project_checkouts` SELECT and UPDATE `(machine_id,
    project_id, root_path)` scoped to the grant machine. file: `crates/gcode/security/managed_postgres_privileges.json`.

    1.1.3: Two issued capabilities on different machines each see only their checkout
    row, can `SELECT ... FOR SHARE` that row, and cannot INSERT/UPDATE/DELETE checkout
    rows. test: `tests/storage/test_postgres_agent_authorization.py`.

    1.1.4: Privilege-manifest parity includes `project_checkouts` SELECT and UPDATE
    `(machine_id, project_id, root_path)` scoped to `machine_id`. test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.

    1.1.5: Regenerated `catalog.manifest.json`, `assets.rs` identities, `schema_expected_identity.json`,
    catalog freshness, and schema-contract tests match staged 375 with `project_checkouts`
    present and `projects.repo_path` still in place. file: `crates/gcore/assets/schema/catalog.manifest.json`.
    test: `crates/gcore/tests/catalog_manifest_freshness.rs`. test: `crates/gcore/tests/schema_contract.rs`.

    1.1.6: Fresh schema installs daemon-runtime, migration-owner, and project-and-machine
    capability SELECT policies plus the lock-only UPDATE grant on `project_checkouts`;
    daemon-role CRUD succeeds; two issued capabilities remain machine-isolated, can
    `FOR SHARE` their row, and cannot mutate checkout rows. test: `tests/storage/test_postgres_agent_authorization.py`.

    1.1.7: Capability `projects` grants include `SELECT (id, name, deleted_at)` plus
    the existing `repo_path` grant; privilege-manifest parity lists those columns;
    live capability authorization executes `projects.deleted_at IS NULL` as `gobby_gcode_capability`.
    file: `crates/gcode/security/managed_postgres_privileges.json`. test: `tests/storage/test_postgres_agent_authorization.py`.
    test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.'
  labels:
  - covers:project-checkout-identity:1.1:1.1.1
  - covers:project-checkout-identity:1.1:1.1.2
  - covers:project-checkout-identity:1.1:1.1.3
  - covers:project-checkout-identity:1.1:1.1.4
  - covers:project-checkout-identity:1.1:1.1.5
  - covers:project-checkout-identity:1.1:1.1.6
  - covers:project-checkout-identity:1.1:1.1.7
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Add checkout storage and typed errors
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: "1.2.1: Lookup, list, idempotent same-root register, conflict,\
    \ root-taken, rebind, and sentinel refusal behave as specified. test: `tests/storage/test_project_checkouts.py`.\n\
    1.2.2: Unix and Windows-style `root_path` strings store and retrieve unchanged\
    \ with no server-side path interpretation. test: `tests/storage/test_project_checkouts.py`.\n\
    1.2.3: `rebind` inserts when absent, no-ops the same root without timestamp mutation,\
    \ updates a different root, and raises `CheckoutRootTakenError` without mutation\
    \ when another project owns the root. Absent-row insert preserves matching or\
    \ empty active index state and leaves mismatched-root cleanup to \xA7 4.2. test:\
    \ `tests/storage/test_project_checkouts.py`.\n1.2.4: `register` returns `(checkout,\
    \ created)` from one INSERT/conflict transaction, and `CHECKOUT_FREE_PROJECT_IDS`\
    \ pins `ORPHANED_PROJECT_ID`, `MIGRATED_PROJECT_ID`, `GLOBAL_PROJECT_ID`, and\
    \ `PERSONAL_PROJECT_ID`. test: `tests/storage/test_project_checkouts.py`.\n1.2.5:\
    \ `register` and `rebind` recheck machine-qualified overlay membership in the\
    \ same transaction and refuse an overlay inserted after `validate_checkout_root`.\
    \ test: `tests/storage/test_project_checkouts.py`.\n1.2.6: Concurrent absent-row\
    \ `rebind` with equal roots produces one insert and typed same-root no-ops; different\
    \ roots serialize through INSERT then `FOR UPDATE` and the loser takes the different-root\
    \ branch. test: `tests/storage/test_project_checkouts.py`."
  labels:
  - covers:project-checkout-identity:1.2:1.2.1
  - covers:project-checkout-identity:1.2:1.2.2
  - covers:project-checkout-identity:1.2:1.2.3
  - covers:project-checkout-identity:1.2:1.2.4
  - covers:project-checkout-identity:1.2:1.2.5
  - covers:project-checkout-identity:1.2:1.2.6
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Resolve ordinary roots from project_checkouts
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: '1.3.1: `require_root` returns the checkout row or raises the
    typed errors above, with no legacy-column fallback, and refuses a foreign `machine_id`
    before checkout lookup or filesystem access. test: `tests/storage/test_project_checkouts.py`.

    1.3.2: Ordinary create/ensure/update call `validate_checkout_root`, write only
    `project_checkouts`, and refuse overlay paths. test: `tests/storage/test_storage_projects.py`.

    1.3.3: `sample_project`, the shared servers fixture, and checkout resolver tests
    use the isolated-machine helper (marker, machine, project, checkout). test: `tests/conftest.py::sample_project`.
    test: `tests/servers/conftest.py`.

    1.3.4: `resolve_operation_root` with `overlay_path=None` uses the primary checkout
    and raises `CheckoutNotFoundError` when that row is missing; a valid registered
    local worktree or clone wins even when no primary checkout exists; every non-null
    unregistered, wrong-project, or foreign-machine overlay is a typed refusal; missing-machine
    and sentinel cases raise the typed errors above; a foreign `machine_id` is refused
    before overlay or checkout lookup. test: `tests/storage/test_project_checkouts.py`.

    1.3.5: `validate_checkout_root` never expands input; it rejects relative paths,
    unexpanded `~`, nonexistent paths, overlays, and marker mismatches, and accepts
    only a platform-local normalized absolute. test: `tests/utils/test_checkout_root.py`.

    1.3.6: Checkout writers call `require_local_machine_id(provided_machine_id, resource_kind="project_checkout",
    resource_id=project_id)` first, pass that returned id into `validate_checkout_root`,
    and cover local, missing, and foreign `provided_machine_id` cases with foreign
    rejection before filesystem access. test: `tests/utils/test_checkout_root.py`.

    1.3.7: A foreign-machine overlay that shares the local candidate string does not
    block a valid local checkout; a same-machine overlay still refuses. test: `tests/utils/test_checkout_root.py`.

    1.3.8: `require_root` and `resolve_operation_root` call `require_local_machine_id(machine_id,
    resource_kind="project_checkout", resource_id=project_id)` after the missing-`machine_id`
    check and before lookup; a foreign session machine is refused before filesystem
    access; `get` and `list_for_machine` still return opaque rows. test: `tests/storage/test_project_checkouts.py`.'
  labels:
  - covers:project-checkout-identity:1.3:1.3.1
  - covers:project-checkout-identity:1.3:1.3.2
  - covers:project-checkout-identity:1.3:1.3.3
  - covers:project-checkout-identity:1.3:1.3.4
  - covers:project-checkout-identity:1.3:1.3.5
  - covers:project-checkout-identity:1.3:1.3.6
  - covers:project-checkout-identity:1.3:1.3.7
  - covers:project-checkout-identity:1.3:1.3.8
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Make init marker-authoritative
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: '2.1.1: The init matrix above holds, including reject-on-existing-name,
    overlay refusal, and sentinel refusal. test: `tests/utils/test_utils_project_init.py`.

    2.1.2: `validate_checkout_root` never expands input; it rejects relative paths,
    unexpanded `~`, nonexistent paths, overlays, and marker mismatches, and accepts
    only a platform-local normalized absolute. test: `tests/utils/test_checkout_root.py`.

    2.1.3: Marker-first then one project-plus-checkout transaction retries after a
    marker-only failpoint without `NameAttachRejectedError`. test: `tests/utils/test_utils_project_init.py`.

    2.1.4: Two concurrent no-marker initializers at one root produce one winning marker
    and project; the loser adopts that ID and does not leave a marker pointing at
    a missing row. test: `tests/utils/test_utils_project_init.py`.

    2.1.5: Marker publication writes a complete fsynced temporary file, installs it
    with no-overwrite, fsyncs the directory, and retries after each publication failpoint
    without exposing a partial marker. test: `tests/utils/test_utils_project_init.py`.

    2.1.6: Two concurrent no-marker initializers at distinct roots with the same unused
    name leave one project; the loser unlinks only its still-matching marker, fsyncs
    the directory, and raises `NameAttachRejectedError`. test: `tests/utils/test_utils_project_init.py`.

    2.1.7: User-invoked init on a valid marker for a soft-deleted project restores
    and registers in one transaction; `rebind` still preserves `deleted_at`. test:
    `tests/utils/test_utils_project_init.py`.

    2.1.8: After a losing-name uniqueness rollback, failpoints before unlink, after
    unlink, and after directory `fsync` cannot resurrect a marker UUID with no project
    row; retry removes only the still-matching losing marker and preserves any later
    replacement marker. test: `tests/utils/test_utils_project_init.py`.

    2.1.9: Two same-root no-marker writers with different explicit names leave one
    deterministic marker `id`/`name`/`created_at` and matching project row. test:
    `tests/utils/test_utils_project_init.py`.

    2.1.10: User-invoked init on a valid marker for a soft-deleted project whose name
    is now active on another UUID raises `NameAttachRejectedError` and rolls back
    restore, register, and marker rewrite. test: `tests/utils/test_utils_project_init.py`.

    2.1.11: No-marker init that publishes a UUID and then loses project-plus-`register`
    to `CheckoutRootTakenError` unlinks only that still-matching marker, fsyncs the
    directory, and retries after rollback, unlink, and directory-fsync failpoints
    without leaving a marker UUID that has no project row. test: `tests/utils/test_utils_project_init.py`.

    2.1.12: No-marker init that publishes a UUID and then loses the same-transaction
    overlay recheck raises `OverlayRegistrationRejectedError`, unlinks only that still-matching
    marker, fsyncs the directory, and retries after rollback, unlink, and directory-fsync
    failpoints without leaving a marker UUID that has no project row. test: `tests/utils/test_utils_project_init.py`.

    2.1.13: No-marker init that publishes a UUID and then loses `validate_checkout_root`
    to `OverlayRegistrationRejectedError` unlinks only that still-matching marker,
    fsyncs the directory, and retries after pre-unlink, post-unlink, and directory-fsync
    failpoints without leaving a marker UUID that has no project row. test: `tests/utils/test_utils_project_init.py`.

    2.1.14: After a committed rename, ID-targeted init or `ensure_exists` on a stale-name
    marker keeps `projects.name` and refreshes only that local marker; it does not
    write the stale marker name into the database. test: `tests/utils/test_utils_project_init.py`.

    2.1.15: Expected-id marker refresh preserves the payload, refuses an `id` change
    without overwrite, fsyncs the temporary file, installs only while the on-disk
    `id` still matches, fsyncs the directory, retries after those failpoints, and
    leaves a concurrent replacement marker untouched. test: `tests/utils/test_utils_project_init.py`.'
  labels:
  - covers:project-checkout-identity:2.1:2.1.1
  - covers:project-checkout-identity:2.1:2.1.2
  - covers:project-checkout-identity:2.1:2.1.3
  - covers:project-checkout-identity:2.1:2.1.4
  - covers:project-checkout-identity:2.1:2.1.5
  - covers:project-checkout-identity:2.1:2.1.6
  - covers:project-checkout-identity:2.1:2.1.7
  - covers:project-checkout-identity:2.1:2.1.8
  - covers:project-checkout-identity:2.1:2.1.9
  - covers:project-checkout-identity:2.1:2.1.10
  - covers:project-checkout-identity:2.1:2.1.11
  - covers:project-checkout-identity:2.1:2.1.12
  - covers:project-checkout-identity:2.1:2.1.13
  - covers:project-checkout-identity:2.1:2.1.14
  - covers:project-checkout-identity:2.1:2.1.15
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Register from hook ingress
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "2.2.1: Non-overlay hook ingress registers the local checkout;\
    \ overlay cwd, isolation copies, and checkout-free sentinel startup do not. test:\
    \ `tests/hooks/test_hook_manager.py`.\n2.2.2: Hook ingress on a valid marker for\
    \ a soft-deleted project refuses without restoring or registering; user-invoked\
    \ init remains the restore path. test: `tests/hooks/test_hook_manager.py`.\n2.2.3:\
    \ Each typed checkout-domain refusal from hook registration propagates to the\
    \ hook ingress boundary; no successful `HookProjectResolution` or session/event\
    \ continuation follows. test: `tests/hooks/test_hook_manager.py`.\n2.2.4: A stale-name\
    \ hook request leaves `projects.name` unchanged, refreshes only that still-matching\
    \ local marker through the \xA7 2.1 expected-id helper, and leaves a concurrently\
    \ replaced marker untouched. test: `tests/hooks/test_hook_manager.py`.\n2.2.5:\
    \ Explicit, session, existing-session, contract-probe, and current-context resolutions\
    \ do not register, rebind, or refresh a marker; only the cwd-marker `ensure_project_in_db`\
    \ path does. test: `tests/hooks/test_hook_manager.py`."
  labels:
  - covers:project-checkout-identity:2.2:2.2.1
  - covers:project-checkout-identity:2.2:2.2.2
  - covers:project-checkout-identity:2.2:2.2.3
  - covers:project-checkout-identity:2.2:2.2.4
  - covers:project-checkout-identity:2.2:2.2.5
  tdd: true
  source_section: '2.2'
  implementation_domain: backend
- title: Add rebind CLI and stop --repo-path
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "2.3.1: Rebind verifies the marker and updates only this machine\u2019\
    s checkout; `--repo-path` is gone; list/show show the local checkout separately.\
    \ test: `tests/cli/test_projects.py`.\n2.3.2: Rebind resolves a unique soft-deleted\
    \ project by UUID or name, preserves `deleted_at`, and changes only that machine\u2019\
    s checkout; ambiguous deleted names require UUID or the PATH marker. test: `tests/cli/test_projects.py`.\n\
    2.3.3: `repair` registers a missing row only when the same-root marker is valid,\
    \ reports creation then, refuses overlay/sentinel/marker-mismatch/invalid-root/conflicting-existing-row\
    \ without persistence, reports no-op for same-root existing rows, and never writes\
    \ `projects.repo_path`. test: `tests/cli/test_projects.py`.\n2.3.4: Rename commits\
    \ `projects.name` with no local checkout; with a local checkout it refreshes only\
    \ that marker through the \xA7 2.1 expected-id helper after commit; a post-commit\
    \ marker-write or `MarkerMismatchError` still leaves the database name changed\
    \ and warns; it never reads `Project.repo_path`. test: `tests/cli/test_projects.py`."
  labels:
  - covers:project-checkout-identity:2.3:2.3.1
  - covers:project-checkout-identity:2.3:2.3.2
  - covers:project-checkout-identity:2.3:2.3.3
  - covers:project-checkout-identity:2.3:2.3.4
  tdd: true
  source_section: '2.3'
  implementation_domain: backend
- title: Expose checkout HTTP and drop repo_path from project JSON
  category: code
  task_type: feature
  depends_on:
  - '2.1'
  validation_criteria: "2.4.1: Project JSON has no `repo_path`, includes the calling\
    \ machine checkout or `checkout: null`, serializes checkout-free sentinels without\
    \ calling `require_root`, and checkout register/rebind reject foreign machine\
    \ ids before filesystem access and reject overlays and marker mismatches. test:\
    \ `tests/servers/routes/test_projects_routes.py`.\n2.4.2: `GET /api/projects/{project_id}/checkouts`\
    \ returns only the calling daemon object-or-null, including 200/`checkout: null`\
    \ for a present sentinel; a second machine\u2019s checkout row is absent from\
    \ the first machine\u2019s response. test: `tests/servers/routes/test_projects_routes.py`.\n\
    2.4.3: Register is 201 then 200 on same-root retry, concurrent same-root requests\
    \ yield exactly one 201 and remaining successes 200, rebind is 200, unavailable\
    \ local machine identity on register and rebind is 409 `MissingMachineContextError`,\
    \ and the C1 typed-error HTTP mapping holds. test: `tests/servers/routes/test_projects_routes.py`.\n\
    2.4.4: Local-checkout settings reads succeed, null-checkout list/get use defaults\
    \ with no filesystem access, settings writes call `require_root`, missing checkout\
    \ is 409, and another machine\u2019s checkout is never used. test: `tests/servers/routes/test_projects_routes.py`.\n\
    2.4.5: HTTP register refuses a soft-deleted project without restoring; HTTP rebind\
    \ preserves `deleted_at` and neither route clears it. test: `tests/servers/routes/test_projects_routes.py`."
  labels:
  - covers:project-checkout-identity:2.4:2.4.1
  - covers:project-checkout-identity:2.4:2.4.2
  - covers:project-checkout-identity:2.4:2.4.3
  - covers:project-checkout-identity:2.4:2.4.4
  - covers:project-checkout-identity:2.4:2.4.5
  tdd: true
  source_section: '2.4'
  implementation_domain: backend
- title: Cut over build and dispatch roots
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '3.1.1: Build ordinary operations use the machine checkout;
    explicit worktree/clone paths still win; missing checkout fails closed. test:
    `tests/build/test_input_resolution.py`.

    3.1.2: Branch cleanup refuses a missing checkout instead of a missing `repo_path`.
    test: `tests/build/test_clean_branches.py::test_branch_cleanup_refuses_missing_project_repo_path`.

    3.1.3: Dispatch spawn and workspace-merge use the machine checkout or a registered
    overlay and fail closed without a checkout. test: `tests/dispatch/test_dispatcher.py`.
    test: `tests/dispatch/test_workspace_merge.py`.'
  labels:
  - covers:project-checkout-identity:3.1:3.1.1
  - covers:project-checkout-identity:3.1:3.1.2
  - covers:project-checkout-identity:3.1:3.1.3
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Cut over agents, sessions, and MCP tool roots
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '3.2.1: Task-path MCP ordinary filesystem work fails closed
    without `(project_id, machine_id)` checkout context, prefers a registered overlay
    when provided, and refuses a foreign session machine before filesystem access.
    test: `tests/mcp_proxy/tools/test_task_repo_paths.py`.

    3.2.2: A session/grant pair with mismatched, missing, or foreign machine identities
    is rejected. test: `tests/storage/test_postgres_agent_authorization.py`.

    3.2.3: Fresh/test `resolve_tool_session` returns `(session_id, project_id, machine_id,
    root_path)` from a `LEFT JOIN` to `project_checkouts` only, permits a null `root_path`
    when no primary checkout exists, and the listed task-tool callers compile against
    the machine-qualified signature. file: `crates/gcore/assets/schema/baseline.sql`.

    3.2.4: `issue_tool_request` accepts a registered local overlay, including when
    the session has no primary checkout, and rejects unregistered, wrong-project,
    and foreign-machine requested paths. test: `tests/storage/test_managed_credentials.py`.

    3.2.5: Fresh/test `resolve_tool_session` returns no row for expired or deleted
    sessions. test: `tests/storage/test_postgres_agent_authorization.py`.

    3.2.6: After installing the four-column `resolve_tool_session` definition, regenerated
    `assets.rs` identities and `schema_expected_identity.json` match that intermediate
    baseline. file: `crates/gcore/src/schema/assets.rs`. file: `src/gobby/storage/schema_expected_identity.json`.

    3.2.7: Lifecycle-monitor, project-context middleware, websocket session, spawn-factory,
    and session-message families resolve the session-machine checkout and fail closed
    on the family''s relevant missing or foreign context. test: `tests/agents/test_lifecycle_monitor.py`.
    test: `tests/servers/test_project_context_middleware.py`. test: `tests/servers/websocket/chat/test_session.py`.
    test: `tests/mcp_proxy/tools/spawn_agent/test_factory.py`. test: `tests/mcp_proxy/test_mcp_tools_session_messages.py`.

    3.2.8: Task-lifecycle coverage tests stop mocking `Project.repo_path` and use
    session-machine checkout or resolver fakes. test: `tests/mcp_proxy/tools/test_task_lifecycle_coverage.py`.
    test: `tests/mcp_proxy/tools/test_tasks_lifecycle_coverage.py`. test: `tests/mcp_proxy/tools/test_task_worktree_lifecycle_decoupling.py`.

    3.2.9: An eligible overlay-only session returns a `resolve_tool_session` row with
    null `root_path`; `issue_tool_request` then authorizes a registered local overlay
    and refuses missing, invalid, and foreign overlays; expired or deleted sessions
    still return no row. test: `tests/storage/test_managed_credentials.py`. test:
    `tests/storage/test_postgres_agent_authorization.py`.'
  labels:
  - covers:project-checkout-identity:3.2:3.2.1
  - covers:project-checkout-identity:3.2:3.2.2
  - covers:project-checkout-identity:3.2:3.2.3
  - covers:project-checkout-identity:3.2:3.2.4
  - covers:project-checkout-identity:3.2:3.2.5
  - covers:project-checkout-identity:3.2:3.2.6
  - covers:project-checkout-identity:3.2:3.2.7
  - covers:project-checkout-identity:3.2:3.2.8
  - covers:project-checkout-identity:3.2:3.2.9
  tdd: true
  source_section: '3.2'
  implementation_domain: backend
- title: Cut over files, source control, plans, and workflows
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: '3.3.1: Files routes resolve the calling machine checkout and
    fail closed when it is missing. test: `tests/servers/routes/test_files.py`.

    3.3.2: Source-control missing checkout is 409, not an empty diff. test: `tests/servers/routes/test_source_control_routes.py`.

    3.3.3: `project_context` and session cwd no longer read `Project.repo_path`. test:
    `tests/utils/test_project_context.py`.

    3.3.4: Skills routes, Linear CLI, and Linear sync resolve a local checkout, fail
    closed without one, and skip `require_root` for checkout-free sentinels. test:
    `tests/servers/routes/test_skills_routes.py`. test: `tests/cli/test_linear_coverage.py`.
    test: `tests/sync/test_linear_sync.py`.

    3.3.5: `GET /api/files/projects` returns checkout-shaped project JSON (`checkout`
    object or `checkout: null`) and never `repo_path`. test: `tests/servers/routes/test_files.py`.

    3.3.6: Plans, wiki, scheduler, dream, workflows, and session-changes resolve a
    local checkout and fail closed or skip sentinels on each family''s relevant missing-checkout
    branch. test: `tests/cli/test_plans.py`. test: `tests/plans/test_handoff_manifest_service.py`.
    test: `tests/plans/test_review_evidence.py`. test: `tests/wiki/test_scope_resolution.py`.
    test: `tests/scheduler/test_cron_executor.py`. test: `tests/memory/test_dream.py`.
    test: `tests/workflows/test_hooks.py`. test: `tests/servers/test_session_changes.py`.

    3.3.7: Session commit-count uses the session machine checkout resolver and covers
    local success, missing checkout, missing machine, and foreign-machine refusal
    before git runs. test: `tests/servers/routes/test_sessions_routes.py::TestGetCommitCount`.

    3.3.8: Workflow dirty-file checks inspect a registered local overlay even when
    no primary checkout exists, use the primary checkout when the candidate is absent
    or equals that checkout, and raise a typed refusal for invalid or foreign overlays.
    test: `tests/workflows/test_hooks.py`.'
  labels:
  - covers:project-checkout-identity:3.3:3.3.1
  - covers:project-checkout-identity:3.3:3.3.2
  - covers:project-checkout-identity:3.3:3.3.3
  - covers:project-checkout-identity:3.3:3.3.4
  - covers:project-checkout-identity:3.3:3.3.5
  - covers:project-checkout-identity:3.3:3.3.6
  - covers:project-checkout-identity:3.3:3.3.7
  - covers:project-checkout-identity:3.3:3.3.8
  tdd: true
  source_section: '3.3'
  implementation_domain: backend
- title: Cut over isolation reconciliation and runner startup
  category: code
  task_type: feature
  depends_on:
  - '1.3'
  validation_criteria: "3.4.1: Isolation reconciliation and runner startup enumerate\
    \ this machine\u2019s checkouts and leave overlay registries intact. test: `tests/test_isolation_reconciliation.py`.\n\
    3.4.2: Recovery ignores a foreign session whose path string matches a local directory.\
    \ test: `tests/storage/tasks/test_live_session_recovery.py`.\n3.4.3: Runner startup\
    \ removes `gobby:wiki-*` jobs when this machine has no checkout or the checkout\
    \ directory is gone, and does not treat overlay paths as missing primaries. test:\
    \ `tests/test_runner_project_recovery.py`."
  labels:
  - covers:project-checkout-identity:3.4:3.4.1
  - covers:project-checkout-identity:3.4:3.4.2
  - covers:project-checkout-identity:3.4:3.4.3
  tdd: true
  source_section: '3.4'
  implementation_domain: backend
- title: Resolve gcode projects through project_checkouts
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  validation_criteria: "4.1.1: Gcode name and id resolution use the local checkout,\
    \ not `projects.repo_path` or another machine\u2019s index root. test: `crates/gcode/src/config/tests.rs`.\n\
    4.1.2: Gcode name lookup matches only `projects.deleted_at IS NULL`; a deleted-only\
    \ name is a miss; the single active checkout wins when deleted duplicates exist;\
    \ UUID or marker resolution remains the explicit non-name path. test: `crates/gcode/src/config/tests.rs`."
  labels:
  - covers:project-checkout-identity:4.1:4.1.1
  - covers:project-checkout-identity:4.1:4.1.2
  tdd: true
  source_section: '4.1'
  implementation_domain: backend
- title: Invalidate only the rebound machine index view
  category: code
  task_type: feature
  depends_on:
  - '2.3'
  validation_criteria: "4.2.1: Different-root rebind removes only the selected machine\u2019\
    s active index states and leaves shared content and other-machine state in place;\
    \ same-root rebind and absent-row insert with no state or a matching recorded\
    \ root do not delete index state; absent-row insert with a different recorded\
    \ root clears that machine/project\u2019s active project and file states. test:\
    \ `tests/storage/test_project_checkouts.py`.\n4.2.2: Crash mid-rebind, a concurrent\
    \ stale-root primary index writer, and a paused post-commit callback cannot expose\
    \ the new checkout with old active state or delete current-root state. test: `tests/storage/test_project_checkouts.py`.\n\
    4.2.3: Full and explicit primary pipelines refuse a root that is not the committed\
    \ checkout; overlay upserts still write view rows. test: `crates/gcode/src/index/indexer/tests/api_contract.rs`.\n\
    4.2.4: The Python project-state upsert refuses a stale primary root and still\
    \ writes overlay-view rows without a checkout. test: `tests/code_index/test_storage.py`.\n\
    4.2.5: The upsert API exposes primary versus overlay mode; every listed direct\
    \ caller, including `refresh_project_stats` and both `crates/gcode/src/commands/status/content_gc/tests.rs`\
    \ `adopt_file_state` cases, chooses a mode explicitly. test: `crates/gcode/src/index/api_tests.rs`.\
    \ test: `crates/gcode/src/commands/status/content_gc/tests.rs`.\n4.2.6: A paused\
    \ primary seed or stats writer holding `FOR SHARE` cannot write old-root state\
    \ after a different-root rebind takes `FOR UPDATE`; overlay mode still writes\
    \ without a checkout lock. test: `crates/gcode/src/index/indexer/tests/api_contract.rs`.\
    \ test: `tests/code_index/test_storage.py`.\n4.2.7: Destination-side discovery\
    \ and hashing run before adopting shared content; the adjacent full-index case\
    \ reparses previously adopted content. test: `crates/gcode/src/index/indexer/tests/serial_db.rs::indexing_adopts_existing_content_version_without_reparse`.\
    \ test: `crates/gcode/src/index/indexer/tests/serial_db.rs::full_indexing_reparses_previously_adopted_content`.\n\
    4.2.8: A paused primary file-state upsert, adopt, delete, or orphan-cleanup writer\
    \ holding `FOR SHARE` cannot recreate old-root active file state after a different-root\
    \ rebind; overlay file writes remain checkout-independent. test: `crates/gcode/src/index/indexer/tests/api_contract.rs`.\n\
    4.2.9: The Python file-state upsert and delete refuse a stale primary root, hold\
    \ `FOR SHARE` through the write, and still write overlay-view rows without a checkout.\
    \ test: `tests/code_index/test_storage.py`."
  labels:
  - covers:project-checkout-identity:4.2:4.2.1
  - covers:project-checkout-identity:4.2:4.2.2
  - covers:project-checkout-identity:4.2:4.2.3
  - covers:project-checkout-identity:4.2:4.2.4
  - covers:project-checkout-identity:4.2:4.2.5
  - covers:project-checkout-identity:4.2:4.2.6
  - covers:project-checkout-identity:4.2:4.2.7
  - covers:project-checkout-identity:4.2:4.2.8
  - covers:project-checkout-identity:4.2:4.2.9
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Replace web repo_path with checkout
  category: code
  task_type: feature
  depends_on:
  - '2.4'
  - '3.3'
  validation_criteria: '5.1.1: Project list, search, files, and BranchIndicator use
    `checkout.root_path` and never read `repo_path`. test: `web/src/hooks/__tests__/useProjects.test.tsx`.
    test: `web/src/hooks/__tests__/useFiles.test.ts`.

    5.1.2: Remaining `ProjectWithStats` and logical HTTP project fixtures, including
    App, SkillsTab, WikiA11y, `useFiles`, and the named Playwright specs, construct
    `checkout` (including null) and never `repo_path`. test: `web/src/components/activity/fields/__tests__/DateTimeField.test.tsx`.
    test: `web/src/__tests__/App.test.tsx`. test: `web/src/components/activity/skills/__tests__/SkillsTab.test.tsx`.
    test: `web/src/components/activity/wiki/__tests__/WikiA11y.test.tsx`. test: `web/src/hooks/__tests__/useFiles.test.ts`.
    test: `web/tests/file-editor.spec.ts`.

    5.1.3: Null-checkout project list, search, and BranchIndicator render without
    throwing and omit path identity. test: `web/src/hooks/__tests__/useProjects.test.tsx`.'
  labels:
  - covers:project-checkout-identity:5.1:5.1.1
  - covers:project-checkout-identity:5.1:5.1.2
  - covers:project-checkout-identity:5.1:5.1.3
  tdd: true
  source_section: '5.1'
  implementation_domain: frontend
- title: Pin two-machine checkout identity
  category: test
  task_type: feature
  depends_on:
  - '2.4'
  - '4.2'
  - '5.1'
  validation_criteria: "5.2.1: Two-machine HTTP, overlay refusal, and one-machine\
    \ rebind compose without leaking the other machine\u2019s checkout or index state.\
    \ test: `tests/integration/test_project_checkout_identity.py`."
  labels:
  - covers:project-checkout-identity:5.2:5.2.1
  tdd: false
  source_section: '5.2'
  assigned_agent: backend-developer
- title: Run the project-checkout-cutover campaign
  category: code
  task_type: feature
  depends_on:
  - '2.2'
  - '3.1'
  - '3.2'
  - '3.3'
  - '3.4'
  - '4.1'
  - '4.2'
  - '5.1'
  validation_criteria: "6.1.1: Successful populated migration, covered-row rerun with\
    \ zero inserts, two valid existing machine checkout rows perform zero inserts\
    \ and preserve both rows and both machines\u2019 index state, zero/multiple unresolved\
    \ rejection only without an authoritative checkout, `no_candidate_machine` abort\
    \ for a non-sentinel legacy-path project with empty checkout and candidate-machine\
    \ sets then abort-then-rebind-then-rerun success, abort-then-rebind-then-rerun\
    \ success for local and foreign machines, transactional rollback, prompt-free\
    \ resume, soft-deleted abort/rebind/rerun, sentinel exclusion, local-validation\
    \ refusal, and receipt verification all pass. test: `tests/storage/test_project_checkout_cutover.py`.\n\
    6.1.2: Target baseline has no `projects.repo_path`, live `resolve_tool_session`\
    \ matches the \xA7 3.2 checkout-only definition, gcode grants no longer select\
    \ `repo_path` while still selecting `deleted_at`, and privilege-manifest parity\
    \ lists `projects` columns `id`, `name`, and `deleted_at` only. file: `crates/gcore/assets/schema/baseline.sql`.\
    \ test: `tests/code_index/test_gcode_privilege_manifest.py::test_manifest_privileges_match_the_managed_relation_set`.\n\
    6.1.3: Post-cutover residue allowlist fails on identity-bearing `repo_path` leftovers\
    \ in production and tests, including positional ordinary-root `create` / `ensure_exists`\
    \ / `update` arguments, and allows only campaign input plus ordinary path locals.\
    \ test: `tests/storage/test_project_checkout_cutover.py::test_identity_repo_path_residue_allowlist`.\n\
    6.1.4: Campaign bootstrap executes `CREATE TABLE IF NOT EXISTS project_checkouts`\
    \ on an already-receipted 375 database, then verifies the complete table, constraint,\
    \ policy, and grant shape, including the 1.1 daemon-runtime, migration-owner,\
    \ and project-and-machine capability SELECT policies plus the lock-only UPDATE\
    \ grant, and does not refresh the receipt. test: `tests/storage/test_project_checkout_cutover.py`.\n\
    6.1.5: Hub-maintenance lazy-load, rehearsal, live, resume, refusal, and operator\
    \ messages work for `project-checkout-cutover`. test: `tests/cli/test_hub_maintenance.py`.\n\
    6.1.6: `verify.rs` and `runner_tests.rs` no longer encode `projects.repo_path`.\
    \ file: `crates/gcore/src/schema/verify.rs`.\n6.1.7: Campaign registry and baseline\
    \ CHECK constraints include `project-checkout-cutover`; `account_identity_cutover.py`\
    \ `_TARGET_CAMPAIGNS` and known-constraint recognition preserve that value, and\
    \ constraint replacement retains the expanded set. test: `tests/storage/test_account_identity_cutover.py`.\n\
    6.1.8: Live `resolve_tool_session` is dropped without `CASCADE` and recreated\
    \ with the \xA7 3.2 four-column `LEFT JOIN` shape, including nullable `root_path`;\
    \ recreation failure rolls back. test: `tests/storage/test_project_checkout_cutover.py`.\n\
    6.1.9: Staged gcore classifies today's live 375 receipt as the project-checkout\
    \ predecessor and the target receipt as already-baselined. test: `crates/gcore/src/schema/runner_tests.rs`.\n\
    6.1.10: After the column drop, regenerated `catalog.manifest.json`, `seed.manifest.json`,\
    \ `assets.rs` identities, `schema_expected_identity.json`, catalog freshness,\
    \ and schema-contract tests match the `repo_path`-free schema. file: `crates/gcore/assets/schema/catalog.manifest.json`.\
    \ file: `crates/gcore/assets/schema/seed.manifest.json`. test: `crates/gcore/tests/catalog_manifest_freshness.rs`.\
    \ test: `crates/gcore/tests/schema_contract.rs`.\n6.1.11: Live and fresh `resolve_tool_session`\
    \ still return no row for expired or deleted sessions after drop-then-create.\
    \ test: `tests/storage/test_postgres_agent_authorization.py`.\n6.1.12: After target-schema\
    \ application, hub-maintenance does not auto-start the installed pre-epic daemon;\
    \ predecessor-only abort may start it; applied-target abort leaves the fence for\
    \ staged-binary resume. test: `tests/cli/test_hub_maintenance.py`.\n6.1.13: Unresolved\
    \ candidates call `require_local_machine_id` before filesystem access, persist\
    \ only through `LocalProjectCheckoutManager.register` in the campaign transaction,\
    \ and `register.created` matches the recorded insert set. test: `tests/storage/test_project_checkout_cutover.py`.\n\
    6.1.14: The production-Python DDL inventory includes exact post-implementation\
    \ operation counts for `src/gobby/storage/project_checkout_cutover.py` and still\
    \ fails on unexpected DDL. test: `tests/storage/test_schema_contract.py::test_production_python_has_no_persistent_postgres_ddl`."
  labels:
  - covers:project-checkout-identity:6.1:6.1.1
  - covers:project-checkout-identity:6.1:6.1.2
  - covers:project-checkout-identity:6.1:6.1.3
  - covers:project-checkout-identity:6.1:6.1.4
  - covers:project-checkout-identity:6.1:6.1.5
  - covers:project-checkout-identity:6.1:6.1.6
  - covers:project-checkout-identity:6.1:6.1.7
  - covers:project-checkout-identity:6.1:6.1.8
  - covers:project-checkout-identity:6.1:6.1.9
  - covers:project-checkout-identity:6.1:6.1.10
  - covers:project-checkout-identity:6.1:6.1.11
  - covers:project-checkout-identity:6.1:6.1.12
  - covers:project-checkout-identity:6.1:6.1.13
  - covers:project-checkout-identity:6.1:6.1.14
  tdd: true
  source_section: '6.1'
  implementation_domain: backend
```
