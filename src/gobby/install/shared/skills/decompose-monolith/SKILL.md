---
name: decompose-monolith
description: Use when an oversized production source file, monolith, god file, or requested module decomposition needs structural refactoring.
version: "1.0.0"
category: engineering
triggers:
  - oversized source file
  - monolith
  - god file
  - module decomposition
  - split large file
sources:
  - https://github.com/affaan-m/ecc/blob/main/skills/orch-refine-code/SKILL.md
  - https://github.com/github/awesome-copilot/blob/main/skills/refactor/SKILL.md
  - https://github.com/wshobson/agents/blob/main/plugins/python-development/skills/python-design-patterns/SKILL.md
metadata:
  gobby:
    audience: all
    depth: 0
---

# Decompose Monolith

Apply this language-neutral workflow to any hand-maintained production source
indexed by `gcode`. Preserve observable behavior throughout the decomposition.

REQUIRED SKILL: code-index.

Direct extraction is the default. Use strangler migration only when old and new
paths must coexist, consumers require staged migration, delivery spans multiple
units, or a routing seam provides material rollback safety.

Read
`get_skill_file(name="decompose-monolith", path="references/architectural-shapes.md")`
when choosing boundaries for a service, parser or compiler, UI or component,
systems module, or stylesheet.

## Workflow

### 1. Confirm Scope and Ceiling

Find the applicable ceiling in repository instructions, rules, and tooling.
If the repository defines none, use 1,000 lines. Apply it to hand-maintained
production source files. Exclude generated, vendored, baseline, documentation,
fixture, and test artifacts only when repository evidence identifies them as
such.

### 2. Establish a Green Characterization Baseline

Run focused tests and relevant runtime checks that pin current behavior,
including error behavior, state transitions, and observable side effects. Add
characterization coverage where coverage is thin, then record a green baseline.
Keep every later change small and behavior-preserving.

### 3. Map Structure and Consumers with `gcode`

Map symbols, dependencies, and callers before choosing a boundary:

```text
gcode outline <file>
gcode imports <file>
gcode usages <symbol-id>
gcode callers <symbol-id>
gcode blast-radius <symbol-name>
```

Resolve symbol IDs before graph queries. Map state ownership, public entry
points, and consumers for each candidate boundary. When a graph command is
unavailable, use indexed `gcode search-symbol`, `gcode grep`, and
`gcode search-content` queries to recover the same evidence.

### 4. Discover Cohesive Boundaries

Group symbols by responsibility, state ownership, reason to change, and
consumers. A candidate module passes the reason-to-change test when its contents
would normally change for the same business or technical cause. Reject groups
based only on adjacency, line ranges, or a desired file size.

### 5. Design the Target Module Graph

Design explicit modules whose responsibilities can be named independently.
The graph must be acyclic, and dependencies must flow toward stable domain
units. Give shared mutable state one explicit owner. Expose narrow operations,
keep implementation details private, and merge tiny fragments that lack an
independent responsibility.

### 6. Select the Migration Strategy

Choose direct extraction when callers and imports can migrate atomically within
one bounded change. Choose strangler migration when implementations must
coexist, consumers migrate independently, work spans multiple delivery units,
or rollback safety justifies a routing seam. Record the evidence for the choice.

### 7. Extract Cohesive Slices

Extract one responsibility at a time. Update its consumers, run focused tests,
compiler or type checks, linting, and relevant runtime checks, then inspect
dependency direction. Continue only from green.

## Direct Extraction

Use this sequence when the change is atomic:

1. Extract one cohesive responsibility.
2. Update consumers.
3. Validate behavior and dependency direction.
4. Repeat until the monolith is gone or reduced to a justified thin
   coordinator.
5. Run final structural and behavioral validation.

A thin coordinator should contain only necessary orchestration and sit
comfortably below the applicable ceiling. Move any policy, state ownership,
formatting, parsing, persistence, transport, or presentation responsibility to
the module that owns it.

## Strangler Migration

Use this sequence when coexistence or staged cutover is required:

1. Define the routing seam, legacy behavior, consumer migration order,
   verification signals, and rollback behavior.
2. Introduce the new module behind the seam.
3. Migrate consumers incrementally, validating each consumer after migration.
4. Verify every consumer uses the new path and required behavior remains
   correct.
5. Perform mandatory post-verification cleanup:
   - delete the legacy implementation;
   - delete temporary routers, adapters, feature flags, and compatibility
     branches;
   - remove obsolete tests, configuration, metrics, and documentation; and
   - collapse any temporary facade whose contract is no longer required.
6. Run a final green validation after cleanup.

A strangler decomposition is incomplete until cutover verification, cleanup,
and final green validation all succeed.

## Structural Completion Criteria

- Every hand-maintained production source file is below the applicable ceiling.
- Each resulting file has an independently describable responsibility.
- Dependencies are acyclic and flow toward stable domain units.
- Shared mutable state has one explicit owner.
- Public surfaces remain minimal.
- Temporary migration machinery has been removed.
- Focused tests, compiler or type checks, linting, and relevant runtime checks
  pass.

Reject arbitrary line-range extraction, generic `utils` or `common` modules,
permanent forwarding shells, circular imports, tiny fragments, and a remaining
near-threshold coordinator.
