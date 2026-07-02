# Dart and Elixir External Callee Provenance Spike

## Decision

Dart and Elixir now have enough parser/import surface for conservative external
callee classification in the safe subsets below. The remaining rule is still
fail-closed: emit external targets only when provenance is explicit, and leave
ambiguous local, receiver, wildcard/imported, or dynamic calls unresolved.

Dart uses textual call extraction because the grammar does not expose a stable
call query shape for every case. Elixir uses tree-sitter call captures plus
explicit Mix/alias provenance.

The minimum bar for future expansion is:

1. Capture import or alias provenance with enough structure to distinguish local, package, and standard-library roots.
2. Add manifest-backed external provenance where imports alone are insufficient.
3. Classify only direct, explicit, module-qualified calls.
4. Preserve local collision checks.

## Dart

### Current Surface

`languages.rs` captures Dart symbols from function, class, method, and enum
declarations. It captures imports through `import_or_export`; `parser.rs`
extracts Dart calls textually and can ask a `SemanticCallResolver` about
otherwise unresolved textual calls.

### Implemented Call Surface

Dart call extraction handles:

- Bare function calls, kept unresolved unless local same-file resolution applies.
- Prefixed calls such as `json.decode(...)`.
- Static or constructor-like qualified calls such as `http.Client(...)` when the prefix maps to an imported package alias.
- Method invocations on receivers, kept unresolved.

The extractor tracks callee name and qualifier path separately. Capturing only
the final identifier is not enough because `decode(...)`, `json.decode(...)`,
and `client.decode(...)` have different provenance.

### Provenance Needed

Dart imports need structured parsing of:

- `import 'dart:convert';`
- `import 'package:http/http.dart' as http;`
- `import './local.dart';`
- `show` and `hide` combinators.

External roots come from:

- `dart:` SDK imports.
- `package:` imports whose package name is present in `pubspec.yaml` dependencies, dev dependencies, or dependency overrides.

Relative imports and `package:` imports that point back to the current package remain local or unresolved.

### Minimum Fail-Closed Path

Classify only:

- `alias.member(...)` where `alias` comes from an explicit external `as alias` import.
- `dartSdkRoot.member(...)` only if the syntax exposes a stable prefix.

Leave unresolved:

- Unqualified calls.
- Receiver methods.
- Calls imported through `show` without an alias.
- Dynamic calls and `Function.apply`.
- Relative imports.

## Elixir

### Current Surface

`languages.rs` captures Elixir functions through `def`, `defp`, and `defmacro`,
modules through `defmodule`, imports through calls to `import`, `alias`, `use`,
and `require`, and call sites through the Elixir `call_query`.

### Current Call Surface

Elixir call extraction covers:

- Remote calls such as `Jason.decode!(body)`.
- Aliased module calls such as `HTTPoison.get(url)`.
- Local calls such as `decode!(body)`, kept unresolved except for local symbol resolution.
- Pipeline calls, where the final AST shape may still be a remote call but arguments are transformed by the pipe operator.

The query must capture module alias/root and function name separately. It also needs to avoid treating macro declarations as ordinary call sites.

### Provenance Needed

External roots come from:

- `mix.exs` dependencies.
- `mix.lock` package resolution.
- Explicit `alias External.Module, as: LocalAlias`.
- Explicit `require External.Module` for macro calls.

Local roots come from project `defmodule` captures. If a local module root collides with a dependency module, leave the call unresolved.

### Minimum Fail-Closed Path

Classify only:

- Direct remote calls where the module root or alias has explicit dependency provenance.
- Macro calls only when `require` provides explicit external provenance.

Leave unresolved:

- Local unqualified calls.
- Calls made available through `import`.
- Calls introduced by `use`.
- Dynamic `apply/3`, computed module aliases, and generated functions.
- Pipeline calls unless the captured remote call target remains structurally explicit.

## Shared Implementation Notes

- Extend `ImportBindings` only with explicit roots or aliases.
- Preserve local-symbol and local-module collision checks.
- Add parser tests for positive external remote calls and fail-closed local, wildcard/imported, receiver, and dynamic cases before widening either language.

_Last verified: 2026-05-24_
