---
name: swift
description: "Enforces default Swift coding standards for agents writing or refactoring Swift: SwiftPM/Xcode configuration, API design, optionals and protocols, structured concurrency, platform boundaries, testing, performance, and memory. Use before editing Swift unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: swift, swiftpm, xcode, swiftui, uikit, appkit, actors, sendable, async-await, swift-testing, xctest, swiftlint, swift-format
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for SwiftPM, Xcode, Swift concurrency, Apple platforms, server Swift, and package workflows."
  - "Secondary: Swift.org documentation for Swift Package Manager, API Design Guidelines, and Swift concurrency; Apple Swift Testing and XCTest documentation."
---

# Swift

Default coding standards for Swift. Repo conventions and configured tooling take
precedence. If `Package.swift`, Xcode build settings, SwiftPM plugins, SwiftLint,
swift-format, platform SDK rules, or project instructions are stricter, follow
the repo.

## Tooling

Run the repo's configured format, lint/static analysis, compile, and focused
tests before finishing. If none are configured, use the local Swift project:

- Format/lint: swift-format, SwiftLint, Xcode scripts, or repo wrappers
- Compile/type checks: targeted `swift build`, `xcodebuild build`, or package
  target build for changed modules
- Tests: focused Swift Testing, XCTest, package, simulator, or framework tests
- Packages: preserve `Package.swift`, `Package.resolved`, Xcode project settings,
  lockfiles, generated files, and toolchain pins
- Runtime checks: simulator, device, server smoke, or platform-specific checks
  where the changed boundary depends on them

Do not relax compiler settings, strict concurrency diagnostics, actor isolation,
Sendable checks, SwiftLint/swift-format rules, dependency pins, or platform
deployment targets to make a quick change pass.

## Configuration

- Match the repo's Swift tools version, package graph, Xcode project, deployment
  targets, compiler flags, language mode, and CI matrix.
- Keep dependency declarations, target membership, resource processing, build
  settings, plugins, and generated code deterministic.
- Prefer the standard library, Foundation, platform SDKs, Swift package APIs, and
  local helpers already in use before adding dependencies.

For SwiftPM, Xcode, compiler flags, SwiftLint, swift-format, packages, and CI:
`get_skill_file(name="swift", path="references/configuration.md")`

## Types And API Design

- Model states with optionals, enums with associated values, structs, protocols,
  generics, and explicit result types instead of loosely typed dictionaries or
  sentinel strings.
- Treat Objective-C, C, JSON, persistence, environment, network, user input, and
  platform callbacks as untrusted boundaries.
- Preserve public API source/binary compatibility, protocol requirements,
  ownership semantics, Sendable contracts, and naming conventions unless the
  change is intentional.

For optionals, value/reference semantics, protocols, generics, API naming, and
boundary modeling:
`get_skill_file(name="swift", path="references/types-and-api-design.md")`

## Concurrency And Error Handling

- Use structured concurrency. Avoid detached tasks, hidden unstructured work,
  swallowed cancellation, blocking calls in async code, and unclear actor
  isolation.
- Preserve error causes and domain context when translating framework, network,
  persistence, process, or platform failures.
- Make actor isolation, MainActor boundaries, Sendable safety, TaskGroup fan-out,
  timeouts, retries, and cancellation behavior explicit and testable.

For async/await, actors, Sendable, cancellation, errors, retries, and cleanup:
`get_skill_file(name="swift", path="references/concurrency-and-error-handling.md")`

## Framework And Platform Boundaries

- Keep SwiftUI, UIKit, AppKit, watchOS/tvOS/visionOS, Vapor, Codable,
  persistence, DI, and Objective-C/C interop boundaries separate from domain
  logic.
- Validate request, persistence, JSON, Keychain, file, environment, C pointer,
  Objective-C, and platform callback data before it reaches core code.
- Preserve target, module, package, app-extension, and platform availability
  boundaries.

For Apple frameworks, server Swift, persistence, Codable, availability, and
interop:
`get_skill_file(name="swift", path="references/framework-and-platform-boundaries.md")`

## Testing

- Add focused tests for changed behavior, error paths, concurrency timing,
  actor isolation, platform boundaries, Codable/persistence, and target behavior.
- Use the repo's stack: Swift Testing, XCTest, swift test, xcodebuild,
  ViewInspector, snapshot tests, simulator/device tests, or server test harnesses.
- Prefer deterministic unit and boundary tests before broad package or scheme
  invocations.

For Swift Testing, XCTest, package tests, simulator tests, and command selection:
`get_skill_file(name="swift", path="references/testing.md")`

## Performance And Memory

- Measure hot paths before optimizing. Check ARC churn, retain cycles,
  copy-on-write behavior, collection shape, task creation, actor hops, main-thread
  work, and bridging costs.
- Keep ownership, mutability, isolation, caching, buffering, and lifetime
  decisions explicit.
- Use lazy sequences, batching, streaming, value types, actors, or unsafe APIs
  only with evidence and tests.

For ARC, value/reference semantics, collections, actor hops, instruments, and
platform runtime behavior:
`get_skill_file(name="swift", path="references/performance-and-memory.md")`

## Before You Finish

If you touched Swift: verify formatting/lint, compile/static analysis, focused
tests, and any relevant simulator, device, package, server, or concurrency checks
pass before closing your work.
