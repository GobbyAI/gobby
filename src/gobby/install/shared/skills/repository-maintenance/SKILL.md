---
name: repository-maintenance
description: Use when creating packages, moving modules, changing package dependencies, introducing shared abstractions, or changing state ownership.
version: "1.0.0"
category: engineering
alwaysApply: false
triggers:
  - repository structure
  - package creation
  - module movement
  - cross-package dependency
  - shared abstraction
  - state ownership
  - top-level package
metadata:
  gobby:
    audience: all
---

# Repository Maintenance

Use this for structural choices that outlive the immediate edit.

## Structural Decision

Before creating or moving structure:

1. Name the capability that owns the behavior.
2. Search established placement and dependency patterns with `gcode`. Use
   `gcode tree`, `gcode search`, `gcode imports`, `gcode usages`, and
   `gcode blast-radius` as the question requires.
3. Write down the intended dependency direction, state owner, public API
   change, and test location.
4. Prefer the smallest boundary that preserves ownership and an acyclic graph.

For production file decomposition, REQUIRED SKILL: decompose-monolith. Delegate
the decomposition judgment to that skill; arbitrary line splits are not
architectural boundaries.

## Healthy Patterns

- Capability-oriented packages group behavior by reason to change.
- Thin adapters translate at transport, CLI, storage, or provider boundaries.
- Dependencies point inward from adapters to stable capability contracts.
- One state owner controls each mutable fact; consumers use a narrow public API.
- Narrow public APIs expose capability operations without deep private imports.
- Colocated or mirrored tests make ownership and coverage visible.
- Generated and vendor content stays separated from hand-maintained source and
  is updated through its source of truth.

## Structural Debt Signals

Stop and reconsider when a proposal introduces:

- `utils` or `common` dumping grounds;
- a one-helper package without a distinct capability;
- deep private imports, dependency cycles, or outward domain dependencies;
- duplicated transport and domain models with no boundary translation;
- arbitrary line splits or forwarding facades that preserve the old coupling;
- manually maintained volatile inventories that a generator can derive.

## Decision Checklists

### Package Creation or Module Movement

- Does the destination own a coherent capability with multiple related changes?
- What existing package owns the behavior, data, and tests?
- Do `gcode` results show an established placement or import direction?
- Will the move create a one-helper package, generic dumping ground, facade, or
  deep private import?
- Can all consumers move while the dependency graph remains acyclic?

### Dependencies, Models, and Public Surface

- Which side is the stable capability and which side is an adapter?
- Can the adapter translate into a domain contract at the boundary?
- Does the change keep dependencies pointing inward?
- Is the public API limited to the operations consumers need?
- Are transport and domain models distinct only where translation has value?

### State, Tests, and Generated Content

- Which component is the single state owner, writer, and source of truth?
- Are other copies immutable projections, or accidental competing owners?
- Do tests belong beside the capability or in its mirrored test package?
- Is generated or vendor content isolated, reproducible, and verified after
  regeneration?

## Maintenance Rule

Do not introduce new structural debt. Keep unrelated cleanup outside the task.
For pre-existing debt, follow active repository policy: fix it inside the task
only when policy requires it or the requested change cannot be completed
correctly without it.

## Gobby Examples

- Task behavior belongs under `src/gobby/tasks/`; an MCP tool remains a thin
  adapter under `src/gobby/mcp_proxy/tools/` and depends inward on the task API.
- A session-name helper stays with `src/gobby/sessions/` unless `gcode` evidence
  proves a separate capability and consumer boundary.
- `src/gobby/install/bundled_content_manifest.json` is a generated inventory.
  Change the shared source, run the existing manifest generator, and verify the
  regenerated output instead of hand-maintaining hashes.
