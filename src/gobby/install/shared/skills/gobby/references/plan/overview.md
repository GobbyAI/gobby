# Planning
Load when investigating or operating a Gobby plan. Load the operation topic before acting; this overview does not satisfy topic requirements.

## Discover and route
Use `gobby-plans:list_plans` and `get_plan` for registered artifacts. Discover unknown tools with `list_tools`; fetch each known unleased tool schema before calling it. Pass caller `session_id` on the outer proxy call. Schemas remain authoritative for parameters.

Investigate the repository and map independently closeable outcomes before choosing a route. One atomic outcome expected to fit one focused implementation session uses the tasks workflow. Multiple dependent deliverables use a plan. Duration alone is not the discriminator.

## Workflow
Load [drafting](drafting.md) and [coverage](coverage.md) for narrative authoring. Resolve material decisions with standalone `elicit` and `restraint`. Preserve research findings in each owning deliverable.
Base validation and explicit user approval are mandatory. [Enhancement](enhancement.md) and [adversarial review](review.md) are optional and require their own authorization. [Approval](approval.md) owns manifest application; [expansion](expansion.md) owns task-tree creation; [lifecycle](lifecycle.md) owns registration and archive. Use [repair](repair.md) for validator residue and interrupted review checkpoints.

## Constraints and recovery
The canonical artifact is `.gobby/plans/<slug>.md`. Provider write restrictions still apply. Staged conversation is not a saved or validated plan. Drafting/review creates no synthetic planning or per-round tasks; registration waits for a real implementation root.
A menu lists choices without starting them. Existing approval remains valid within its authorized scope; a continued drafting request does not imply implementation approval.
On unknown IDs inspect registered plans and task ancestry. On stale evidence stop applying it and follow repair guidance; never fabricate hashes, attestations, or a manifest.

See [Plans and plan mode](../../../../../../../../docs/guides/plans-and-plan-mode.md#mental-model).

_Last verified: 2026-09-12_
