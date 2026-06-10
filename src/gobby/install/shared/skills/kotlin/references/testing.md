# Testing

Use this reference when choosing tests and validation for Kotlin changes.

## Test Levels

- Unit tests for pure domain logic, API contracts, mapping, validation, and
  error handling.
- Coroutine tests for suspend functions, Flow, cancellation, retry, timeout,
  dispatcher, and lifecycle behavior.
- JVM/framework integration tests when serialization, persistence, Ktor, Spring,
  DI, or real HTTP boundaries matter.
- Android tests with Robolectric or instrumentation when lifecycle, resources,
  permissions, Compose, or platform APIs are the behavior.
- KMP target tests for changed platform source sets.

## Tools

- Use the repo's stack: kotlin-test, JUnit 4/5, Kotest, MockK, Mockito,
  kotlinx-coroutines-test, Turbine, Robolectric, AndroidX test, Ktor test host,
  Spring test, Testcontainers, WireMock, MockWebServer, or local helpers.
- Mock external systems, clocks, dispatchers, network clients, and platform
  boundaries. Avoid mocking internals when a fake or real value object is clearer.
- Prefer parameterized tests for state matrices and boundary cases.

## Coroutine Tests

- Use `runTest` and test dispatchers. Do not rely on real delays or scheduler
  timing.
- Assert cancellation and failure behavior. Verify `CancellationException` is not
  swallowed.
- Use Turbine or equivalent helpers for Flow emissions, completion, errors, and
  backpressure.

## Command Selection

Run the narrowest command that proves the changed behavior:

- `./gradlew :module:test --tests com.acme.ProfileClientTest`
- `./gradlew :module:compileKotlin`
- `./gradlew :module:detekt :module:ktlintCheck`
- `./gradlew :app:testDebugUnitTest --tests '*ProfileViewModelTest'`
- `./gradlew :shared:jvmTest :shared:iosSimulatorArm64Test`

Broaden to module `check`, Android lint, or platform tests when wiring,
resources, generated code, or source-set changes require it.

## Assertions

- Assert observable behavior and error shape, not private call order.
- Cover invalid inputs, null/platform data, retries, cancellation, serialization
  compatibility, and boundary failures.
- Keep tests deterministic: fake clocks, controlled dispatchers, temporary
  directories, stable ports, and isolated state.
