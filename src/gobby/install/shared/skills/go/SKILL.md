---
name: go
description: "Enforces default Go coding standards for agents writing or refactoring Go: module configuration, API contracts, error handling, testing, concurrency, and performance. Use before editing Go unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: go, golang, go.mod, go.sum, go.work, golangci, gofmt, go vet, govulncheck, testing, benchmark
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Go modules and toolchains."
  - "Secondary: Go project conventions around modules, Effective Go, code review comments, testing, and error handling."
---

# Go

Default coding standards for Go. Repo conventions and configured tooling take precedence. If `go.mod`, `go.work`, `.golangci.*`, generated-code rules, framework rules, or project instructions are stricter, follow the repo.

## Tooling

Run the repo's configured format, lint, vet, vulnerability, and test commands before finishing. If none are configured, use:

- Format: `gofmt -w` for touched files and `go fmt ./...` for packages when safe
- Lint: configured `golangci-lint run`, or targeted linters already used by the repo
- Vet: `go vet ./...` or the narrow package set touched
- Tests: targeted `go test ./path/...`, with `-race` for concurrency-sensitive changes
- Vulnerabilities: `govulncheck ./...` when dependency or network-facing code changes

Do not add unchecked goroutines, panic-based control flow, ignored errors, broad generated-code edits, or global mutable state without a written reason tied to an external boundary or migration step.

## Configuration

- Keep module and workspace files deterministic: `go mod tidy`, `go work sync`, and vendoring only when the repo already uses it.
- Match the repo's Go version and `toolchain` directive; do not bump either incidentally.
- Keep package boundaries small and named for what they provide, not for implementation layers.
- Prefer standard library facilities before adding dependencies, especially for HTTP, JSON, logging, sync, and testing.

For modules, package layout, and lint setup: `get_skill_file(name="go", path="references/configuration.md")`

## Types And API Contracts

- Model domain concepts with named types or small structs instead of passing raw strings and maps through layers.
- Accept interfaces at boundaries only when multiple implementations exist or tests need a seam.
- Return concrete types from constructors and package-private helpers unless an interface is part of the public contract.
- Validate JSON, environment, CLI, file, and network data before trusting fields.

For struct, interface, generic, and validation patterns: `get_skill_file(name="go", path="references/types.md")`

## Error Handling

- Check every returned error and wrap with `%w` when adding useful context.
- Use `errors.Is` and `errors.As` for sentinel or typed errors across package boundaries.
- Keep panics at startup, programmer-error, or impossible-state boundaries; recover only at process or request isolation points.
- Close resources with deferred cleanup and preserve cleanup errors when they matter.

For error contracts and boundary patterns: `get_skill_file(name="go", path="references/error-handling.md")`

## Testing

- Add table-driven tests for branch-heavy logic and named cases for boundary behavior.
- Use `t.Helper`, `t.Cleanup`, `httptest`, `fstest`, `testdata`, and subtests to keep tests readable.
- Test error paths, cancellation paths, and malformed external data, not only happy paths.
- Run package-local tests first, then broaden only as needed by dependencies.

For Go test patterns and command selection: `get_skill_file(name="go", path="references/testing.md")`

## Concurrency

- Pass `context.Context` as the first argument when work can be canceled, timed out, or request-scoped.
- Prefer `errgroup`, worker pools, or bounded semaphores for fan-out work.
- Own channel closing on the sending side and document goroutine lifetimes.
- Use `time.Timer`, `time.Ticker`, locks, and atomics with explicit cleanup and race-test coverage.

For goroutines, cancellation, channels, and cleanup: `get_skill_file(name="go", path="references/async.md")`

## Performance

- Benchmark before optimizing and keep benchmark data with the change when performance is the reason.
- Avoid avoidable allocations in hot loops, parsers, serializers, and network paths.
- Prefer streaming `io.Reader`/`io.Writer` APIs for large data.
- Use pooling, preallocation, and custom encoders only after profiling shows the cost.

For profiling, allocation, and hot-path patterns: `get_skill_file(name="go", path="references/performance.md")`

## API And Design

- Keep package public APIs minimal; every exported identifier needs a stable contract and doc comment.
- Use small functions with explicit dependencies instead of hidden package globals.
- Keep generated files clearly marked and regenerate from the source tool, not manual patches.
- Use comments for exported identifiers, invariants, concurrency ownership, or non-obvious error behavior.

## Before You Finish

If you touched Go: verify formatting, targeted tests, vet/lint where configured, and race or vulnerability checks when the change makes those relevant.
