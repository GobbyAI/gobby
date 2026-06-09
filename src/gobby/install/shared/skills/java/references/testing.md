# Java Testing

Use this reference when adding or changing Java tests, fixtures, build commands,
mocking strategy, or framework integration coverage.

## Test Scope

- Start with the narrowest test that proves the changed behavior.
- Add unit tests for pure domain logic, parsing, validation, error translation,
  and collection behavior.
- Add integration tests when correctness depends on serialization, database
  mapping, HTTP clients, messaging, Spring/Jakarta wiring, transactions, or
  generated code.
- Run broad module/build tasks after focused tests when dependency wiring or
  framework scanning changed.

## JUnit

- Prefer JUnit 5 if the repo uses it.
- Use parameterized tests for branches and boundary inputs.
- Use `@TempDir`, fake clocks, deterministic random seeds, and test data
  builders to keep tests reproducible.
- Assert exception type, message shape, cause, and externally visible state when
  error behavior matters.

## Mocking

- Mock external systems, clocks, network clients, queues, and persistence
  adapters.
- Avoid mocking value objects, records, collections, and the class under test.
- Use Mockito or the repo's existing mocking library sparingly; a fake is often
  clearer for stateful collaborators.
- Do not loosen visibility just for tests unless the repo already accepts that
  pattern.

## Framework Tests

- Prefer slice tests for Spring MVC, JSON, repository, or configuration behavior
  when a full application context is unnecessary.
- Use Testcontainers or local test services when SQL dialect, broker behavior,
  or network protocol compatibility matters.
- Keep context startup costs visible; do not replace a fast unit test with a
  full context test unless framework wiring is the behavior.

## Commands

- Gradle examples:
  - `./gradlew :service:test --tests com.acme.profile.ProfileClientTest`
  - `./gradlew :service:check`
- Maven examples:
  - `./mvnw -pl service -am test -Dtest=ProfileClientTest`
  - `./mvnw -pl service -am verify`

## Assertions

- Assert observable behavior, not private implementation.
- Include malformed input, null/empty boundaries, dependency failures,
  cancellation/interruption paths, and serialization round trips when relevant.
- Keep test names specific enough to explain the behavior under review.
