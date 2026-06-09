# C# Configuration

Use the repo's .NET topology before adding packages or new projects.

## SDK And Solution Shape

- Check `global.json` for pinned SDK version and roll-forward policy.
- Keep projects inside the existing solution layout unless the repo already uses
  loose project references.
- Match target frameworks, runtime identifiers, `LangVersion`,
  `ImplicitUsings`, `Nullable`, trimming, AOT, and single-file settings.
- Respect shared props/targets such as `Directory.Build.props`,
  `Directory.Build.targets`, and `Directory.Packages.props`.

## Dependencies

- Preserve central package management, `packages.lock.json`, and restore policy.
- Do not upgrade analyzer, SDK, source-generator, or runtime packages unless the
  task is specifically about that upgrade.
- Prefer BCL and existing dependencies before adding packages for validation,
  mapping, time, logging, HTTP, JSON, DI, or configuration.
- Keep package versions compatible across multi-targeted projects.

## Analyzer And Style Configuration

- Treat `.editorconfig`, rulesets, StyleCop, Roslyn analyzers, and
  warnings-as-errors as build contracts.
- Fix warnings in touched code rather than suppressing them.
- Use `#pragma warning disable` only for narrow generated-code or interop cases,
  with a comment explaining the boundary.

## Generated Code

- Keep generated files deterministic and separated from hand-written code.
- Regenerate source generators, OpenAPI clients, protobuf, EF migrations, or
  Razor output only when the change affects their inputs.
- Do not edit generated files by hand unless the repo explicitly stores patched
  generated output.

## Commands

Prefer repo wrappers. Without repo wrappers, use focused commands:

```bash
dotnet restore path/to/Project.csproj
dotnet format path/to/Project.csproj --include path/to/TouchedFile.cs
dotnet build path/to/Project.csproj
```
