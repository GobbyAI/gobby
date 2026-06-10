# Types And API Design

Use this reference when changing public APIs, models, protocols, generics,
optionals, Codable contracts, or module boundaries.

## Optionals And State

- Represent absence with optionals and real states with enums or typed results.
  Avoid sentinel strings, loosely typed dictionaries, and parallel booleans.
- Unwrap at boundaries with `guard`, pattern matching, or throwing initializers.
  Do not scatter force unwraps through core logic.
- Keep invalid external data outside domain models. Decode into boundary types,
  validate, then map into stronger domain values.

## Value, Reference, And Ownership

- Prefer structs for simple values and classes for identity, shared mutable state,
  Objective-C inheritance, or framework requirements.
- Make mutability visible with `let`, `var`, `mutating`, copy-on-write wrappers,
  actors, or isolated services.
- Avoid hidden retain cycles in closures, delegates, notifications, Combine,
  async tasks, and UI callbacks. Capture `self` deliberately.

## Protocols And Generics

- Use protocols to model behavior at module boundaries. Do not create protocols
  solely to mock one concrete type unless the boundary is real.
- Keep associated types, existentials, opaque result types, and generic
  constraints understandable at call sites.
- Prefer explicit dependency injection over global singletons for services,
  clocks, file systems, HTTP clients, schedulers, and persistence.

## API Shape

- Follow Swift API Design Guidelines: clear names at use sites, first argument
  labels that read naturally, and mutating/nonmutating behavior that matches
  expectations.
- Preserve public source and binary compatibility when packages or frameworks
  expose APIs to external callers.
- Keep throwing, async, actor-isolated, Sendable, availability, and ownership
  contracts explicit in signatures.

## Boundary Contracts

- Validate JSON, property lists, user defaults, Keychain values, environment,
  files, network responses, C pointers, Objective-C objects, and platform
  callbacks before trusting them.
- Avoid exposing Foundation dictionaries or raw platform objects across domain
  boundaries when typed models are available.
- Keep Codable compatibility changes deliberate: defaults, renamed fields,
  migration paths, and failure behavior need tests.
