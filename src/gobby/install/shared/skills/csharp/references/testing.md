# C# Testing

Keep tests focused on behavior and use the repo's existing test framework.

## Framework And Shape

- Use xUnit, NUnit, MSTest, Verify, FluentAssertions, Shouldly, Moq, NSubstitute,
  WebApplicationFactory, Testcontainers, or fixture helpers only when the repo
  already uses them.
- Follow Arrange, Act, Assert. One behavior per test.
- Name tests by behavior, including the condition and expected outcome.
- Prefer test data builders over large object initializers repeated in every
  test.

## Unit Tests

- Unit-test pure domain logic, validators, mappers, result handling, and small
  services without ASP.NET Core hosting or databases.
- Mock external boundaries such as HTTP, clocks, queues, file systems, and
  gateways. Avoid mocking internal collaborators when a real object is cheap.
- Assert observable behavior: return values, state changes, emitted messages,
  stored records, logs at boundaries, and exceptions.

## Integration Tests

- Use integration tests when behavior depends on DI registration, middleware,
  routing, model binding, authorization, EF Core provider behavior, SQL,
  transactions, or configuration.
- Prefer real providers for database behavior. SQLite/in-memory replacements can
  hide translation, constraint, and concurrency problems.
- Reset state between tests with transactions, Respawn, container recreation, or
  repo-provided fixtures.

## Async And Time

- Make async tests return `Task`; do not block on async operations.
- Control clocks, GUIDs, random values, locale, and environment variables.
- Avoid sleeps. Use explicit synchronization, fake clocks, or bounded polling.

## Commands

Use repo wrappers when present. Without wrappers, target the touched project or
test names:

```bash
dotnet test tests/Project.Tests/Project.Tests.csproj --filter FullyQualifiedName~BehaviorName
dotnet test tests/Project.Tests/Project.Tests.csproj --no-restore --no-build
```
