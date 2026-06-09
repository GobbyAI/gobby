# Go Configuration Reference

Keep Go configuration boring and reproducible. Module, workspace, lint, and generated-code decisions are repository contracts.

## Modules

- Do not change the `go` directive or `toolchain` directive unless the task requires a toolchain migration.
- Run `go mod tidy` after dependency changes and inspect both `go.mod` and `go.sum`.
- Do not vendor dependencies unless the repo already uses vendoring.
- Preserve `replace` directives unless the task explicitly removes local development wiring.

## Workspaces

Use `go.work` only when the repo already has a workspace or the task is explicitly workspace-level. After changing workspace membership, run `go work sync` and verify package-local tests still resolve the intended modules.

## Formatting And Lint

- `gofmt` is mandatory for touched Go files.
- Use `goimports` when the repo already uses it; otherwise keep imports gofmt-compatible.
- Prefer repo-configured `golangci-lint run` over inventing a new lint command.
- Keep generated files out of manual edits and run their generator instead.

## Package Layout

- Name packages for behavior: `parser`, `storage`, `auth`, `queue`.
- Avoid `common`, `util`, and `helpers` packages unless the repo already has a clear convention.
- Keep internal packages under `internal/` when the API must stay private to the module.
- Put command entrypoints under `cmd/<name>` and keep business logic in importable packages.

## Dependencies

Prefer standard library packages for HTTP, JSON, logging, synchronization, and tests unless a dependency already owns that concern in the repo. New dependencies need a clear reason: protocol support, compatibility, performance, or maintained domain logic.
