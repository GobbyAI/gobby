# Plan registration, hashes and archive
Load before registering, refreshing, archiving or deleting plan metadata.

## Discover
Use gobby-plans:list_plans with project/state/kind filters and get_plan by plan ID or root reference. Resolve the canonical file and owning project's checkout. The DB registry owns state; Markdown owns the narrative.

## Register and maintain
create_plan takes stable plan_id, plan_path and implementation/strategy kind. Supply the real root_task_ref explicitly; inference from a filename is not authorization to invent a planning anchor. Register only after a real implementation root exists. Implementation registration generates managed coverage; strategy has none.
Use update_plan_hash after authorized narrative changes; regenerate_coverage_manifest repairs derived implementation coverage. These tools do not approve changed content. Revalidate and preserve the approval/expansion contract before execution.
There is no gobby-tasks-ops:update_plan_hash tool. Refresh the registered plan through gobby-plans:update_plan_hash, then run expansion QA to refresh task artifact pointers; use task artifact tools only with their exact coupled-field contract.

## Archive and delete
archive_plan preserves history, moves the file to .gobby/plans/completed/, marks archived_at/state and removes managed coverage. Prefer it for completed work. delete_plan hard-deletes the registry row and managed manifest; it is for invalid/accidental records and does not imply deleting the narrative file.
Inspect current state, root closure, filesystem collisions and scope before mutation. On failure, inspect actual registry/file state and resolve the specific path/hash/identity issue; do not blindly repeat a partial lifecycle change. Reactivation is explicit where supported; do not create duplicate plan identities.

## Operator surface
The plans CLI exposes list, show, register, validate, archive, review-evidence and review-runs. review-runs prints an expansion-QA pointer, not review-run history; review-evidence provides that inspection surface. Agents use MCP for lifecycle writes.
There is no standalone public plans HTTP registry route; operator HTTP plan-mode behavior is part of chat/session approval and build requests. Inspect the actual route schema rather than inventing /api/plans endpoints.

See [Plan records](../../../../../../../../docs/guides/plans-and-plan-mode.md#plan-records) and [Archive and delete](../../../../../../../../docs/guides/plans-and-plan-mode.md#archive-and-delete).

_Last verified: 2026-09-12_
