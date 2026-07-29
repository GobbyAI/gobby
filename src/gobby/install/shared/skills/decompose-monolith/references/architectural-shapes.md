# Architectural Shapes

Use these responsibility maps as prompts. Derive the actual boundaries from
symbol relationships, state ownership, reasons to change, and consumers.

## Service

Separate domain policy and invariants, use-case orchestration, persistence and
external I/O, request or response translation, and state or cache ownership.
Direct dependencies toward stable domain policy. Keep transport and storage
details from becoming domain dependencies.

## Parser or Compiler

Consider source normalization, token or byte decoding, syntax construction,
semantic analysis, transformation, emission, and diagnostics as distinct
reasons to change. Give the intermediate representation one explicit owner and
keep later phases from importing earlier implementation details.

## UI or Component

Distinguish state and lifecycle ownership, event interpretation, domain
decisions, rendering, and transport or persistence effects. Preserve one
explicit owner for shared mutable state and keep rendering consumers dependent
on narrow state and operation surfaces.

## Systems Module

Separate protocol or binary representation, state machines, resource lifecycle,
concurrency control, platform adapters, and diagnostics. Characterize ordering,
lifetime, memory, cancellation, and concurrency behavior before moving code.

## Stylesheet

Group tokens and global foundations, shell, layout, component rules, feature
states, and media, print, or accessibility adaptations. Map cascade,
specificity, inheritance, custom-property ownership, and selector consumers.
Each resulting file still needs a named responsibility.

## Direct Extraction Example

An oversized service owns domain rules, serialization, and orchestration.
Characterization checks pin behavior, and `gcode` shows its callers can migrate
in one bounded change. Extract the domain rules first, then serialization,
updating consumers and validating after each step. Leave only necessary
orchestration in the coordinator and confirm the final graph is acyclic.

## Strangler Example

Independently deployed consumers share an implementation and require a rollback
path. Define a routing seam and verification signals, introduce the replacement
behind it, then migrate and verify consumers one at a time. After every consumer
uses the new path, delete the legacy implementation, seam, adapters, flags,
obsolete checks, and any unnecessary facade. Run final validation after cleanup.
