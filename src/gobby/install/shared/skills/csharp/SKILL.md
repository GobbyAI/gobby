---
name: csharp
description: "Enforces default C# and .NET coding standards for agents writing or refactoring C#: SDK/project configuration, nullable types, async/error boundaries, focused tests, ASP.NET Core/DI boundaries, data access, serialization, performance, and tooling. Use before editing C# unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: csharp, c#, dotnet, csproj, sln, nullable, async, asp.net, efcore, xunit
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for C#, .NET SDK projects, analyzers, ASP.NET Core, dependency injection, and data-access workflows."
  - "Secondary: .NET project conventions around nullable reference types, async APIs, analyzers, test projects, host boundaries, EF Core, and System.Text.Json."
---

# C#

Apply repository SDK, analyzer, target-framework, DI, and generated-code rules first.

## Tooling

- Use the checked-in SDK and configured restore, format, build, analyzer, and
  targeted `dotnet test` commands.
- Preserve NuGet lockfiles, central package management, and generated files.

## Configuration

- Match `global.json`, project properties, nullable mode, implicit usings,
  runtime identifiers, trimming, AOT, and analyzer severity.
- Diagnostic hook: treat nullable and analyzer findings as API-contract evidence;
  fix flow or annotations before using `!`, disabling nullable, or suppressing a rule.

For SDK, project, analyzer, and NuGet setup:
`get_skill_file(name="csharp", path="references/configuration.md")`

## Type And API Contracts

- Model domain states with records, enums, value objects, and typed results.
- Preserve public source and binary contracts across assemblies and serializers.

For nullable contracts and API shape:
`get_skill_file(name="csharp", path="references/types.md")`

## Error Handling

- Translate transport, database, parsing, authorization, and concurrency failures
  at their owning boundary while preserving causes and stack traces.
- Avoid sync-over-async, swallowed exceptions, and unowned fire-and-forget work.

## Concurrency

- Make `Task`, `ValueTask`, `IAsyncEnumerable`, cancellation, timeout, and
  disposal behavior part of the method contract.
- Propagate cancellation and bound concurrent work.

For async APIs and exception mapping:
`get_skill_file(name="csharp", path="references/async-and-errors.md")`

## Testing

- Use the repository's xUnit, NUnit, MSTest, Verify, or host-test stack.
- Choose integration coverage when DI, serialization, persistence, or middleware
  behavior is the contract under change.

For fixtures, fakes, filters, and commands:
`get_skill_file(name="csharp", path="references/testing.md")`

## Framework Boundaries

- Keep controllers, middleware, hosted services, and DI registration as adapters.
- Validate HTTP, options, auth, queue, file, and external-service data at entry.

For ASP.NET Core, DI, and hosting:
`get_skill_file(name="csharp", path="references/framework-boundaries.md")`

## Data And Serialization

- Preserve EF/Dapper/SQL migration history, transactions, concurrency tokens,
  tenant boundaries, DTOs, and wire compatibility.

For persistence and serializers:
`get_skill_file(name="csharp", path="references/data-and-serialization.md")`

## Performance

- Inspect allocations, LINQ materialization, query shape, async overhead, and
  serialization before applying spans, pooling, compiled queries, or caching.

For runtime diagnostics and tuning:
`get_skill_file(name="csharp", path="references/performance.md")`
