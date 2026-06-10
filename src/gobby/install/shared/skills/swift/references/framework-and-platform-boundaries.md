# Framework And Platform Boundaries

Use this reference when changing SwiftUI, UIKit, AppKit, watchOS/tvOS/visionOS,
server Swift, persistence, networking, Codable, Objective-C/C interop, or
platform availability.

## UI Boundaries

- Keep SwiftUI views declarative and small. Move parsing, networking, persistence,
  and policy decisions into models or services.
- Keep UIKit/AppKit controllers focused on lifecycle, binding, and navigation.
  Push domain logic behind typed interfaces.
- Use `@MainActor` for UI-owned state. Do not update UI from background actors or
  detached tasks.
- Preserve preview, simulator, accessibility, localization, and app-extension
  constraints when UI code changes.

## Server And Package Boundaries

- Keep request parsing, authentication, authorization, persistence, and external
  service clients at explicit boundaries.
- Validate inbound data before constructing domain values.
- Keep package targets independent: server-only dependencies should not leak into
  reusable libraries, CLI targets, or shared models.
- Make shutdown, signal handling, connection pools, and background jobs
  cancellation-aware.

## Persistence And Codable

- Treat Codable, Core Data, SwiftData, SQLite, GRDB, file storage, UserDefaults,
  and Keychain as boundary layers.
- Add migrations or compatibility defaults when stored data changes shape.
- Keep encoding/decoding failure behavior explicit. Do not swallow corrupt data
  unless the recovery policy is intentional and tested.

## Objective-C And C Interop

- Treat Objective-C nullability, dynamic dispatch, selectors, KVO, notifications,
  delegates, and C pointers as unsafe boundaries.
- Validate pointer lifetimes, ownership transfer, thread affinity, and error
  codes before wrapping them in Swift APIs.
- Avoid leaking raw `AnyObject`, `NSDictionary`, `UnsafePointer`, or Objective-C
  callback shapes into domain code.

## Availability And Targets

- Use `@available`, conditional compilation, and target checks deliberately.
- Keep platform-specific code behind adapters, extensions, or target-specific
  source files.
- Test the actual platform or simulator when availability, entitlements,
  resources, app extensions, or SDK behavior changes.
