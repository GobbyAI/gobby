---
name: go
description: "Enforces default Go coding standards for agents writing or refactoring Go: module configuration, API contracts, error handling, testing, concurrency, and performance. Use before editing Go unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: go, golang, go.mod, go.sum, go.work, golangci, gofmt, go vet, govulncheck, testing, benchmark
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Go modules and toolchains."
  - "Secondary: Go project conventions around modules, Effective Go, code review comments, testing, and error handling."
---

# Go

Apply repository module, toolchain, linter, generated-code, and package rules first.

## Tooling

- Use `gofmt`, configured linters, `go vet`, focused `go test`, race checks for
  concurrency changes, and `govulncheck` for relevant dependency or network work.

## Configuration

- Preserve Go and toolchain versions, module/workspace boundaries, vendoring policy,
  build tags, generated files, and lockstep `go.mod` or `go.sum` changes.
- Diagnostic hook: investigate compiler, vet, and staticcheck findings at the named
  value or lifetime; avoid blank-identifier discards and `//nolint` without a reason.

For modules, packages, and lint setup:
`get_skill_file(name="go", path="references/configuration.md")`

## Types And API Contracts

- Use named domain types and small structs instead of raw strings and maps.
- Accept interfaces where callers need a real substitution seam; return concrete types.
- Validate JSON, environment, CLI, file, and network input before domain use.

For struct, interface, generic, and validation patterns:
`get_skill_file(name="go", path="references/types.md")`

## Error Handling

- Check returned errors, wrap with `%w` when context helps, and use `errors.Is` or
  `errors.As` across package boundaries.
- Keep panics at programmer-error or process-isolation boundaries and preserve
  meaningful cleanup errors.

For error contracts:
`get_skill_file(name="go", path="references/error-handling.md")`

## Testing

- Use table cases for branch-heavy logic and repository helpers such as `t.Cleanup`,
  `httptest`, `fstest`, subtests, and `testdata`.
- Exercise cancellation, malformed external data, and resource ownership where changed.

For test patterns and commands:
`get_skill_file(name="go", path="references/testing.md")`

## Concurrency

- Pass `context.Context` first for request-scoped or cancelable work.
- Bound fan-out, assign channel-closing ownership, document goroutine lifetimes, and
  clean up timers, tickers, locks, and atomics.

For goroutines, cancellation, channels, and cleanup:
`get_skill_file(name="go", path="references/async.md")`

## Performance

- Use benchmark and profile evidence for allocation, parser, serializer, or network
  changes; reserve pooling and custom encoding for demonstrated costs.

For profiling and hot paths:
`get_skill_file(name="go", path="references/performance.md")`

## API Design

- Keep exported surfaces small, documented, and stable.
- Prefer explicit dependencies over package globals.
- Regenerate marked files from their source tool.
