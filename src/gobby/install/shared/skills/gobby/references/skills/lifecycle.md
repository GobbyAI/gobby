# Manage installed skill state

Load before changing scope, visibility, metadata, deletion or restoration.
Use `get_skill(..., brief=false)` and complete its pages to inspect identity and
provenance. Prefer IDs for mutations when names can resolve differently by
scope. MCP `move_skill_to_project(skill_id, target_project_id)` and
`move_skill_to_installed(skill_id)` preserve identity and report scope conflicts;
a bundled template source cannot be moved into project scope.

`remove_skill` soft-deletes; `restore_skill` restores by ID or name. Inspect the
result and rediscover in the intended scope. Soft-delete retention defaults to
30 days; do not assume a purged row is restorable. Operator HTTP DELETE is
stronger: a second delete of an already-deleted row permanently purges it.
Never repeat that request as a harmless retry. HTTP export returns one
SKILL.md body, not a backup of scripts/references/assets or every management
field; preserve the complete inventory separately.

Operator CLI enable/disable and meta get/set/unset manage DB state directly.
They are not equivalent to loading or uninstalling instructions. Current
name-based local CLI operations resolve global rows; use scoped/ID management
for a project override. Direct get_skill can still retrieve disabled skills;
disabling primarily affects discovery/selection, not an authorization boundary.

Bundled sync refreshes Gobby-owned content and files, preserves the enabled
choice for ordinary drift, and re-enables a restored deleted bundle. It retires
removed Gobby-owned entrypoints through soft deletion and preserves custom
sources. Project rows pointing into bundled template trees are stale shadows
and are purged; maintain genuine custom copies outside those trees. Inspect
sync errors and installed rows before claiming cutover complete.

HTTP create/update/file editing and restore-defaults are operator management
surfaces. File writes update an existing attached path; list before writing.
They do not produce tracked instruction loads. Keep routine lifecycle and
upgrade probes isolated from the user's running daemon.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#lifecycle-and-http-management).

_Last verified: 2026-09-12_
