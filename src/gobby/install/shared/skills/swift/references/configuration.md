# Configuration

Use this reference before changing Swift build files, compiler flags, package
dependencies, target membership, Xcode settings, plugins, or CI.

## Project Shape

- Identify the build system first: Swift Package Manager, Xcode project,
  workspace, generated project, Bazel/Tuist/XcodeGen wrapper, server Swift
  package, or platform app target.
- Prefer checked-in wrappers and repo scripts: `swift`, `xcodebuild`, `make`,
  `mise`, `asdf`, `mint`, `tuist`, `xcodegen`, or CI helper scripts.
- Match the configured Swift tools version, language mode, Xcode version,
  deployment target, SDK, package graph, simulator/device matrix, and CI matrix.
- Keep package targets, products, resources, plugin use, test targets, and Xcode
  target membership explicit.

## Dependencies

- Preserve `Package.resolved`, dependency pins, binary targets, plugin versions,
  generated project files, and dependency update policy.
- Do not add packages for functionality already covered by the standard library,
  Foundation, platform SDKs, Swift packages already in use, or local helpers.
- Keep product/target dependencies narrow. Avoid leaking app-only frameworks into
  reusable libraries or common package targets.
- Treat package updates as behavior changes: record why the new version is needed
  and run tests that cover the affected target.

## Compiler And Static Analysis

- Preserve strict concurrency diagnostics, language mode, warnings-as-errors,
  availability checks, library evolution, testability, optimization levels,
  SwiftLint, swift-format, custom scripts, and build plugins.
- Treat `@preconcurrency`, `@unchecked Sendable`, `nonisolated`, `@available`,
  disabled lints, and compiler-flag changes as design decisions. Keep them narrow
  and explain why the local rule is wrong for the changed code.
- Prefer target-local settings and package plugins over duplicated shell snippets.

## Commands

Choose focused commands that compile and test the changed target:

- `swift build --target ProfileCore`
- `swift test --filter ProfileClientTests`
- `xcodebuild build -scheme ProfileApp -destination 'platform=iOS Simulator,name=iPhone 16'`
- `xcodebuild test -scheme ProfileApp -only-testing:ProfileTests/ProfileSyncTests`
- `swiftlint lint Sources/ProfileCore`
- `swift-format lint --recursive Sources Tests`

Record exact commands and explain why they cover the changed module, target,
platform, and toolchain.
