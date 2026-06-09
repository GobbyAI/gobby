---
name: csharp
description: "Enforces default C# and .NET coding standards for agents writing or refactoring C#: SDK/project configuration, nullable types, async/error boundaries, focused tests, ASP.NET Core/DI boundaries, data access, serialization, performance, and tooling. Use before editing C# unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: csharp, c#, dotnet, csproj, sln, nullable, async, asp.net, efcore, xunit
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for C#, .NET SDK projects, analyzers, ASP.NET Core, dependency injection, and data-access workflows."
  - "Secondary: .NET project conventions around nullable reference types, async APIs, analyzers, test projects, host boundaries, EF Core, and System.Text.Json."
---

# C#

Default coding standards for C# and .NET. Repo conventions and configured
tooling take precedence. If `.editorconfig`, project files, analyzers, test
fixtures, DI setup, or architecture docs are stricter, follow the repo.

## Tooling

Run the repo's configured restore, format, build, analyzer, and focused test
commands before finishing. If none are configured, use:

- Format: `dotnet format` scoped to touched projects or the repo wrapper
- Build/analyze: `dotnet build` for touched solutions or projects
- Tests: targeted `dotnet test --filter ...` or project-specific wrappers
- Packages: preserve the repo's NuGet lockfile, central package management,
  SDK pinning, and analyzer policy

Do not loosen analyzers, nullable settings, language version, warnings-as-errors,
or package constraints to make a change pass.

## Configuration

- Match `global.json`, solution, project, central package, lockfile, and
  `Directory.Build.*` conventions before adding files or packages.
- Keep target frameworks, runtime identifiers, implicit usings, nullable mode,
  trimming/AOT settings, and analyzer configuration intentional.
- Prefer platform and BCL APIs before adding dependencies for DI, mapping,
  validation, serialization, logging, or HTTP.

For SDK, solution, project, analyzer, NuGet, and generated-code setup:
`get_skill_file(name="csharp", path="references/configuration.md")`

## Type And API Contracts

- Treat nullable reference types as API contracts. Avoid `!`, `object`, `dynamic`,
  weak dictionaries, and nullable properties unless the boundary requires them.
- Model domain states with records, discriminated-result types, enums, value
  objects, and explicit options instead of sentinel values and boolean flags.
- Keep public APIs stable; preserve binary/source compatibility where packages,
  plugins, generated clients, or public SDKs depend on them.

For nullable contracts, records, generics, value objects, and API shape:
`get_skill_file(name="csharp", path="references/types.md")`

## Async And Error Handling

- Make `Task`, `ValueTask`, `IAsyncEnumerable`, cancellation, and timeouts
  explicit at I/O boundaries.
- Translate transport, database, parsing, authorization, and concurrency failures
  where dependency context is available.
- Preserve stack traces; avoid swallowed exceptions, sync-over-async, fire-and-
  forget work, and ambient cancellation leaks.

For async APIs, cancellation, exception mapping, and result boundaries:
`get_skill_file(name="csharp", path="references/async-and-errors.md")`

## Testing

- Add focused tests for changed behavior, failure paths, serialization,
  validation, DI registration, middleware/controller behavior, and data access.
- Use the repo's framework: xUnit, NUnit, MSTest, Verify, WebApplicationFactory,
  Testcontainers, or fixture wrappers already present.
- Prefer narrow unit tests for pure logic and integration tests when behavior
  depends on ASP.NET Core hosting, EF Core providers, configuration, or DI.

For C# test project setup, fixtures, fakes, filters, and command selection:
`get_skill_file(name="csharp", path="references/testing.md")`

## Framework Boundaries

- Keep controllers, minimal APIs, background services, middleware, hosted
  services, and DI registrations thin. Put domain decisions behind typed
  services or handlers.
- Validate HTTP, options, auth, route, file, queue, and external service input
  at the boundary before it enters domain code.
- Do not hide business behavior inside service locators, global state, static
  singletons, filters, or framework callbacks.

For ASP.NET Core, DI, options, hosting, background services, and middleware:
`get_skill_file(name="csharp", path="references/framework-boundaries.md")`

## Data And Serialization

- Keep EF Core, Dapper, raw SQL, migrations, JSON, protobuf, and message
  contracts explicit. Avoid letting persistence or wire models become domain
  models by accident.
- Preserve migration history, concurrency tokens, transaction scope, tenant
  filters, and serializer compatibility.
- Validate untrusted payloads before mapping into domain types.

For EF Core, migrations, SQL, DTOs, and serializer compatibility:
`get_skill_file(name="csharp", path="references/data-and-serialization.md")`

## Performance

- Profile before optimizing. Check allocations, LINQ materialization, async
  overhead, serialization, database query shape, and logging hot paths.
- Prefer clear code, then use spans, pooling, compiled queries, caching, and
  source generators only where measurements justify them.

For allocations, query shape, async overhead, and runtime diagnostics:
`get_skill_file(name="csharp", path="references/performance.md")`
