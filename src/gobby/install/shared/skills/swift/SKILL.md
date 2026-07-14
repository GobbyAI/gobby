---
name: swift
description: "Enforces default Swift coding standards for agents writing or refactoring Swift: SwiftPM/Xcode configuration, API design, optionals and protocols, structured concurrency, platform boundaries, testing, performance, and memory. Use before editing Swift unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: swift, swiftpm, xcode, swiftui, uikit, appkit, actors, sendable, async-await, swift-testing, xctest, swiftlint, swift-format
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for SwiftPM, Xcode, Swift concurrency, Apple platforms, server Swift, and package workflows."
  - "Secondary: Swift.org documentation for Swift Package Manager, API Design Guidelines, and Swift concurrency; Apple Swift Testing and XCTest documentation."
---

# Swift

Apply repository SwiftPM/Xcode, platform, compiler, lint, and generated-code rules first.

## Tooling

- Use configured swift-format or SwiftLint, targeted `swift build` or `xcodebuild`,
  focused Swift Testing or XCTest, and relevant simulator/device checks.

## Configuration

- Preserve tools version, package graph, target membership, deployment targets,
  build settings, resources, availability, and generated files.
- Diagnostic hook: treat optional, isolation, and Sendable findings as ownership
  evidence; avoid `!`, `@unchecked Sendable`, and unsafe isolation escapes.

For SwiftPM, Xcode, flags, lint, and CI:
`get_skill_file(name="swift", path="references/configuration.md")`

## Types And API Design

- Model states with optionals, enums, structs, protocols, and constrained generics.
- Preserve public source/binary compatibility and Objective-C exposure where promised.
- Normalize C, Objective-C, JSON, persistence, environment, and network values at entry.

For Swift types and API naming:
`get_skill_file(name="swift", path="references/types-and-api-design.md")`

## Error Handling

- Translate framework, network, persistence, and platform failures at owned adapters,
  preserving causes and cleanup.
- Keep cancellation distinct from domain failure.

## Concurrency

- Use structured tasks, actors, explicit `MainActor` boundaries, bounded task groups,
  and lifecycle-owned work.
- Make Sendable requirements, cancellation, retry, and actor hops visible in contracts.

For concurrency and error boundaries:
`get_skill_file(name="swift", path="references/concurrency-and-error-handling.md")`

## Framework And Platform Boundaries

- Keep SwiftUI/UIKit/AppKit, persistence, Codable, Keychain, C pointers, Vapor, and
  app extensions behind platform-aware adapters.
- Preserve target, module, package, availability, and lifecycle constraints.

For Apple and server framework edges:
`get_skill_file(name="swift", path="references/framework-and-platform-boundaries.md")`

## Testing

- Use repository Swift Testing, XCTest, package, scheme, simulator, or device targets
  at the boundary being changed.
- Control clocks, executors, actors, persistence, and platform fixtures.

For Swift test selection:
`get_skill_file(name="swift", path="references/testing.md")`

## Performance And Memory

- Inspect ARC churn, retain cycles, copies, actor hops, rendering, allocation, and
  I/O for affected workloads.
- Keep ownership and lifetime explicit around unsafe or framework-managed resources.

For Instruments, ARC, and runtime analysis:
`get_skill_file(name="swift", path="references/performance-and-memory.md")`
