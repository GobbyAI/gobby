---
name: objc
description: "Enforces default Objective-C coding standards for agents writing or refactoring Objective-C and Objective-C++: build modes, ARC/MRC ownership, blocks, Foundation APIs, Swift and C-family interop, focused testing, runtime behavior, performance, and security. Use before editing .m, .mm, .h, or .pch files unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: objective-c, objective-c++, objc, clang, arc, mrc, foundation, blocks, xcode, xctest, swift-interop, core-foundation, objective-c-runtime
sources:
  - "Primary: Clang Objective-C Automatic Reference Counting specification, independently summarized for Gobby: https://clang.llvm.org/docs/AutomaticReferenceCounting.html"
  - "Primary: Apple Programming with Objective-C guide for objects, properties, blocks, Foundation conventions, and errors: https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/ProgrammingWithObjectiveC/"
  - "Primary: Apple Advanced Memory Management Programming Guide for retained legacy MRC code: https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/MemoryMgmt/"
  - "Primary: Apple Swift interoperability documentation for imports, nullability, and lightweight generics: https://developer.apple.com/documentation/swift/importing-objective-c-into-swift https://developer.apple.com/documentation/swift/designating-nullability-in-objective-c-apis https://developer.apple.com/documentation/swift/using-imported-lightweight-generics-in-swift"
  - "Seed topic coverage: G1Joshi/Agent-Skills Objective-C checklist; independently expanded and corrected against the primary sources above: https://github.com/G1Joshi/Agent-Skills/blob/main/skills/languages/objective-c/SKILL.md"
---

# Objective-C

Default coding standards for Objective-C and Objective-C++. Repository
conventions and configured tooling take precedence. First identify the target,
compiler mode, SDK, deployment target, ARC setting, per-file compiler flags,
language mode, and Swift/C/C++ boundary.

## Tooling

Run the repository's configured format, static analysis, build, and focused tests
before finishing. Use existing schemes, destinations, configuration files, and CI
wrappers:

- Format: clang-format or repository tooling with the checked-in configuration
- Compile/static analysis: the owning Xcode target, Clang warnings, and analyzer
- Tests: focused XCTest or repository-native unit and integration targets
- Packages/projects: preserve Xcode project membership, modules, generated
  headers, dependency locks, and compiler settings
- Runtime checks: the relevant simulator, device, macOS host, service, or library
  harness

Keep warnings, analyzer findings, ownership diagnostics, deployment targets, and
nullability checks intact. Fix the underlying violation.

## Configuration And Language Modes

- Determine ARC or MRC for the changed target and each changed source file.
  Per-file `-fobjc-arc` or `-fno-objc-arc` flags can override target settings.
- Treat `.m` as Objective-C and `.mm` as Objective-C++. Interpret headers in the
  language context of every consumer.
- Match the repository's Clang version, SDK, modules, prefix headers, language
  standard, warning policy, availability range, and generated-code boundaries.

For target inspection, ARC flags, Objective-C++, headers, modules, Xcode settings,
availability, dependencies, and generated sources:
`get_skill_file(name="objc", path="references/configuration-and-language-modes.md")`

## Ownership And Lifetimes

- Express ownership deliberately for object pointers, block pointers, delegates,
  Core Foundation values, out parameters, and C handles.
- Under ARC, let the compiler insert retains and releases while still breaking
  ownership cycles and respecting bridging rules.
- Under MRC, balance owned objects on every path and preserve the repository's
  established autorelease and `dealloc` conventions.

For ARC/MRC detection, property attributes, ownership qualifiers, autorelease
pools, Core Foundation bridging, delegates, and cleanup:
`get_skill_file(name="objc", path="references/ownership-and-lifetimes.md")`

## Blocks And Concurrency

- Copy blocks that escape their defining scope. Define the execution queue,
  callback lifetime, cancellation behavior, and completion cardinality.
- Audit stored blocks and asynchronous callbacks for owner/block cycles. A local
  weak reference helps only when the complete ownership graph supports it.
- Keep mutable state synchronized with the repository's queue, lock, actor, or
  confinement model. Preserve UI main-thread requirements.

For block storage, captures, weak/strong promotion, MRC differences, GCD, queues,
autorelease pools, cancellation, and completion tests:
`get_skill_file(name="objc", path="references/blocks-and-concurrency.md")`

## Foundation And API Design

- Follow Cocoa naming, initializer, property, collection, equality, copying, and
  delegation conventions already used by the public API.
- Model recoverable failures with the repository's `NSError`, result object, or
  callback convention. Reserve Objective-C exceptions for programming errors.
- Validate external collection shapes and object classes before use. Preserve
  mutability and copy contracts across API boundaries.

For Foundation value types, typed collections, `NSError`, exceptions, KVC/KVO,
copying, initializers, and API naming:
`get_skill_file(name="objc", path="references/foundation-and-api-design.md")`

## Swift And C-Family Interop

- Treat public headers as typed boundaries. Add accurate nullability, lightweight
  generics, ownership, availability, and naming information.
- Verify how Swift imports changed declarations. Compile or test a representative
  Swift caller for Swift-facing API changes.
- Keep C++ implementation details behind Objective-C++ facades or narrow C APIs;
  keep public headers consumable by every declared client language.

For bridging and umbrella headers, modules, nullability, generics, imported names,
Objective-C++, C APIs, Core Foundation, and Swift-facing tests:
`get_skill_file(name="objc", path="references/swift-and-c-family-interop.md")`

## Testing And Tooling

- Add focused tests for success, error, nil, ownership, callback, queue, and
  cross-language behavior affected by the change.
- Build the owning target with its real flags. Avoid standalone compiler commands
  that omit SDK, module, prefix-header, generated-header, or per-file settings.
- Use focused schemes, test plans, and `-only-testing` selectors before broader
  workspace validation.

For XCTest, `xcodebuild`, target selection, Clang diagnostics, analyzer,
sanitizers, leak checks, and mixed Swift/Objective-C tests:
`get_skill_file(name="objc", path="references/testing-and-tooling.md")`

## Runtime Performance And Security

- Measure allocations, autorelease growth, messaging, bridging, synchronization,
  and collection work before optimizing.
- Treat selectors, class names, archives, format strings, KVC keys, notifications,
  and dynamic runtime hooks as trust boundaries when influenced externally.
- Keep swizzling, associated objects, unsafe pointers, and runtime reflection
  narrow, documented, and covered by regression tests.

For Instruments, runtime dispatch, swizzling, associated objects, secure coding,
format strings, dynamic inputs, and unsafe APIs:
`get_skill_file(name="objc", path="references/runtime-performance-and-security.md")`

## Before You Finish

If you touched Objective-C: verify the actual memory-management and language
modes, formatting/static analysis, target build, focused tests, and relevant
Swift, C, C++, simulator, device, or host boundary checks.
