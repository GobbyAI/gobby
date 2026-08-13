# #19651 — Implement Path-Independent Project Identity

**Plan ID:** path-independent-project-identity

## Summary
`kind: framing`

Promote #19651 to an epic under #17435. Replace global `projects.repo_path` with machine-owned checkout records while preserving `.gobby/project.json.id` as the global project identity.

Sequence: `#19651 → #18902 → #17678 → #19664`. Repository-content/view redesign remains owned by #17678.

## Implementation
`kind: framing`

### P1: Checkout persistence and resolution [category: code]
`kind: deliverable`

- Add `project_checkouts` with `machine_id`, `project_id`, opaque `root_path`, and timestamps.
- Enforce primary key `(machine_id, project_id)` and unique `(machine_id, root_path)`, with cascading project/machine foreign keys.
- Add `LocalProjectCheckoutManager` operations for lookup, listing, idempotent registration, required-root resolution, and explicit rebind.
- Remove `repo_path` from the `Project` model and project persistence APIs.
- Add a resolver requiring `(project_id, machine_id)`. Missing context, missing checkout, conflicting roots, and marker mismatches produce typed errors.
- Update PostgreSQL grants, RLS, and session-principal resolution so machine-scoped capabilities see only their checkout.

**Acceptance:**

- P1.1 - Persistence, resolution, typed errors, grants, and RLS implement the machine-owned checkout contract. behavior: `checkout persistence and resolution`.

### P2: Registration, CLI, and HTTP contracts [category: code] (depends: P1)
`kind: deliverable`

- Make `.gobby/project.json.id` authoritative during initialization and hook ingress.
- Register a missing checkout from `gobby init`, local daemon startup for `_personal`, or authenticated local hooks. Same-root retries remain idempotent; copied markers at a second ordinary root raise a conflict.
- Reject name-only attachment when a marker is absent and the name already exists.
- Add `gobby projects rebind PROJECT_REF [PATH]`; default to the current directory, resolve locally, verify the marker, and atomically rebind.
- Remove `gobby projects update --repo-path`. Project list/show commands display the current machine’s checkout as a separate field.
- Keep project HTTP responses logical and path-free. Add:
  - `GET /api/projects/{project_id}/checkouts`
  - `POST /api/projects/{project_id}/checkouts`
  - `POST /api/projects/{project_id}/checkouts/{machine_id}/rebind`
  - `GET|PUT /api/projects/{project_id}/checkouts/{machine_id}/settings`
- Registration and rebinding require verified identity for the named machine. Filesystem-backed settings execute only on that machine’s daemon.

**Acceptance:**

- P2.1 - Initialization, hooks, CLI, and HTTP expose identity-verified checkout registration and rebinding. behavior: `checkout registration and rebinding`.

### P3: Consumer and indexing cutover [category: code] (depends: P2)
`kind: deliverable`

- Thread machine context through build, dispatch, agent spawning, sessions, MCP tools, workflows, plans, file APIs, source control, maintenance, and project configuration.
- Preserve explicit managed worktree/clone roots as the first resolution choice; ordinary project operations use the primary checkout.
- Change `gobby_agent_auth.resolve_tool_session` to join the session’s machine and project to its checkout.
- Update Gcode project-name and project-ID resolution to join `project_checkouts` using the local machine ID.
- Keep `code_indexed_project_states.root_path` as index-view metadata for overlay support; canonical state must agree with the registered checkout.
- Rebind deletes only the affected `(machine_id, project_id)` project/file active states. Shared parsed content, vectors, graph data, and other machines remain intact.
- Preserve destination-side discovery and hashing before shared-content adoption.

**Acceptance:**

- P3.1 - All filesystem consumers resolve a machine-qualified checkout while managed overlays and shared indexed content retain their specified behavior. behavior: `consumer and indexing cutover`.

### P4: One-off fenced cutover [category: code] (depends: P3)
`kind: deliverable`

- Add a `project-checkout-cutover` campaign using the existing maintenance epoch, backup, evidence, resume, and verification framework.
- For every non-empty legacy path, derive candidate machines from machine-owned project evidence such as sessions, index states, worktrees, and clones. Require exactly one candidate.
- Current live preflight must produce four checkout rows on the canonical MBP, including `_personal` and the soft-deleted project. `_global`, `_migrated`, and `_orphaned` remain checkout-free.
- In one transaction: lock relevant tables, revalidate preflight evidence, insert checkout rows, verify exact coverage, remove `projects.repo_path`, update SQL functions/grants, and write the target baseline receipt last.
- Rehearse against the epoch backup, run the live cutover with matching binaries, verify schema identity and checkout coverage, then release the maintenance fence.
- Regenerate baseline catalog, seed manifest, expected schema identity, and Rust/Python contract fixtures.

**Acceptance:**

- P4.1 - The fenced campaign migrates exactly the proven legacy checkout coverage, verifies the target schema, and records its receipt transactionally. behavior: `project-checkout-cutover campaign`.

## Test Plan
`kind: framing`

- Two machines register different roots for the same marker and share one logical project.
- Same-machine registration is idempotent for the same root and conflicts for another root or another project using that root.
- Unix and Windows root strings are preserved exactly by hub storage.
- Initialization rejects missing, malformed, mismatched, and ambiguous markers.
- Filesystem operations fail when machine context or checkout is missing.
- HTTP mutation rejects machine-identity mismatches; logical project payloads contain no checkout path.
- Rebind invalidates only the selected machine’s active index state and preserves shared content plus other-machine state.
- Gcode reuses matching content only after destination-side hashing; managed overlays retain existing behavior.
- Cutover tests cover successful populated migration, zero/multiple candidate rejection, transactional rollback, prompt-free resume, soft-deleted projects, sentinel rows, and receipt verification.
- Run focused protected pytest files, scoped Ruff/Mypy checks, Gcode tests, and Gcore schema-contract tests. Skip the full pytest suite.

## Assumptions
`kind: framing`

- Current trusted hub defines the global project namespace; hosted tenancy remains outside #19651.
- `project_id` stays the sole repository/project UUID.
- Each machine has one ordinary primary checkout per project; managed overlays remain unlimited.
- Hub-side paths are opaque metadata; local daemons own path normalization and filesystem access.
- Pre-0.5 cutover uses one maintenance campaign with current-data assertions and no reusable legacy-mapping interface.
- #17678 owns future separation of canonical repository content identity from index-view identity.
