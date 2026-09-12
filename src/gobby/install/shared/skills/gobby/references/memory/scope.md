# Memory ownership and visibility

Load when an entry is missing across projects or when changing ownership or global
visibility. Inspect `get_memory` and the caller project before mutation. Ordinary
MCP create/search/list use current project context, falling back to the personal
project when absent. Do not assume every maintenance tool inherits that scope:
check its schema and pass an explicit project where supported.

`is_global` controls cross-project visibility; `project_id` remains the owner.
`promote_memory_to_global` exposes an owned entry globally;
`demote_memory_from_global` restricts it to its owner;
`move_memory` changes ownership to a concrete `new_project_id`. These operations
require current-project ownership. Seeing another project's global memory does
not authorize editing it. Moving does not mean promoting.

Use `restore_memory` for an owned soft-hidden row whose identity is known. Ordinary
agent reads omit hidden rows. A not-found response can indicate scope or visibility,
not deletion; inspect the appropriate operator inventory before assuming loss.
Do not bypass ownership through direct SQL, HTTP, or another session identity.

Operator/client surfaces differ: CLI commands supporting `--project` use that
explicit filter, and omission can broaden reads; HTTP uses its documented request
scope and current-project mutation checks. Resolve project references before
using these surfaces for operator work. Agent persistent memory stays on MCP.

Guide: [Scope](../../../../../../../../docs/guides/memory.md#scope).

_Last verified: 2026-09-12_
