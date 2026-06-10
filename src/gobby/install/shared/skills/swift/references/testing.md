# Testing

Use this reference when adding or updating Swift tests, choosing validation
commands, or proving platform behavior.

## Test Stack Selection

- Use Swift Testing when the package or Xcode version already supports it.
- Use XCTest for existing XCTest suites, UI tests, performance tests, older
  toolchains, or framework integration that still depends on XCTest.
- Use package tests for package logic and xcodebuild tests for Xcode schemes,
  simulator/device behavior, app targets, UI tests, entitlements, and resources.
- Use focused snapshot, accessibility, preview, integration, or server harnesses
  only when they cover behavior that unit tests cannot.

## What To Cover

- Test changed behavior, validation, error paths, decoding/migration, actor
  isolation, cancellation, retry, timeout, and platform boundary behavior.
- Add regression tests for previously broken optionals, interop nullability,
  Sendable violations, Codable compatibility, retain cycles, or task lifetimes.
- For public packages, include tests that prove source API behavior and any
  important generic/protocol contracts.

## Async And Concurrency Tests

- Prefer deterministic fakes for clocks, HTTP clients, stores, file systems,
  queues, and platform services.
- Keep async tests bounded with explicit expectations or timeouts.
- Test cancellation by cancelling the owning task or task group and asserting
  cleanup.
- Avoid sleeps as synchronization. Use continuations, injected clocks, actors, or
  controlled fakes.

## Command Selection

Choose the smallest command set that proves the changed behavior:

- `swift test --filter ProfileSyncTests`
- `swift test --filter ProfilePackageTests/profileDecode`
- `xcodebuild test -scheme ProfileApp -only-testing:ProfileTests/ProfileViewModelTests`
- `xcodebuild test -scheme ProfileAppUITests -destination 'platform=iOS Simulator,name=iPhone 16'`
- `swift test --enable-code-coverage` when coverage is required by the repo

Avoid broad `swift test` or all-scheme xcodebuild runs when a focused target
proves the change. Record any simulator, device, or server dependency.

## Assertions

- Assert typed values, structured errors, actor-isolated state, persisted data,
  emitted events, and observable UI state.
- Avoid asserting implementation details such as private helper call order unless
  the order is the behavior.
- Keep tests independent of wall-clock timing, network services, local user
  defaults, Keychain state, and the developer's selected simulator.
