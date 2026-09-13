# Planning
Load when investigating or operating a Gobby plan. Load the operation topic before acting; this overview does not satisfy topic requirements.

## Discover and route
Use `gobby-plans:list_plans` and `get_plan` for registered artifacts. Discover unknown tools with `list_tools`; fetch each known unleased tool schema before calling it. Pass caller `session_id` on the outer proxy call. Schemas remain authoritative for parameters.

Investigate the repository and map independently closeable outcomes before choosing a route. One atomic outcome expected to fit one focused implementation session uses the tasks workflow. Multiple dependent deliverables use a plan. Duration alone is not the discriminator.

Before finalizing either route, load standalone `restraint` and `elicit`, use
elicit's grill-me method, and confirm the Decision Record with the user. Preserve
already confirmed decisions and authorization rather than asking for them again.

For atomic work, reuse and refine an existing matching task rather than creating
a duplicate. Hand off a task-ready contract: scope, exact targets, acceptance
criteria, verification, dependencies, research context and confirmed decisions.
Use the existing task workflow directly; do not create a plan artifact, planning
task, manifest, registry row, or expansion run for this route. Before handoff,
audit each proposed mechanism against restraint's ladder and its concrete consumer.

## Workflow
Load [drafting](drafting.md) and [coverage](coverage.md) for narrative authoring. Resolve material decisions with standalone `elicit` and `restraint`. Preserve research findings in each owning deliverable.
Base validation and explicit user approval are mandatory. [Enhancement](enhancement.md) and [adversarial review](review.md) are optional and require their own authorization. [Approval](approval.md) owns manifest application; [expansion](expansion.md) owns task-tree creation; [lifecycle](lifecycle.md) owns registration and archive. Use [repair](repair.md) for validator residue and interrupted review checkpoints.

## Constraints and recovery
The canonical artifact is `.gobby/plans/<slug>.md`. Provider write restrictions still apply. Staged conversation is not a saved or validated plan. Drafting/review creates no synthetic planning or per-round tasks; registration waits for a real implementation root.
Unattended `gobby build` retains its stage-manifest sequence, installed review
policy and configured round counts. Do not inject interactive menus into it.
A menu lists choices without starting them. Existing approval remains valid within its authorized scope; a continued drafting request does not imply implementation approval.
On unknown IDs inspect registered plans and task ancestry. On stale evidence stop applying it and follow repair guidance; never fabricate hashes, attestations, or a manifest.

See [Plans and plan mode](../../../../../../../../docs/guides/plans-and-plan-mode.md#mental-model).

_Last verified: 2026-09-12_
