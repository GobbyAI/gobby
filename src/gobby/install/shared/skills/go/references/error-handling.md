# Go Error Handling Reference

Errors are part of Go API design. Preserve context for operators while keeping callers able to branch on stable error contracts.

## Checking And Wrapping

Check every returned error. Wrap with `%w` when context helps the caller or operator.

```go
data, err := client.Fetch(ctx, id)
if err != nil {
    return nil, fmt.Errorf("fetch profile %s: %w", id, err)
}
```

Do not wrap errors with `%v` when callers need `errors.Is` or `errors.As`.

## Sentinel And Typed Errors

- Use sentinel errors for stable states such as not found, conflict, or unauthorized.
- Use typed errors when callers need structured details.
- Keep sentinels and typed errors near the package API that returns them.
- Compare through `errors.Is` and `errors.As`, not direct equality, across package boundaries.

## Panics

Panics are acceptable for programmer errors, impossible internal states, and startup misconfiguration that cannot be recovered locally. Request, job, and goroutine boundaries should convert panics to failures only when the process can continue safely.

## Cleanup

Use `defer` for cleanup, but do not hide cleanup failures that matter.

```go
func writeFile(path string, data []byte) (err error) {
    f, err := os.Create(path)
    if err != nil {
        return fmt.Errorf("create %s: %w", path, err)
    }
    defer func() {
        if closeErr := f.Close(); closeErr != nil && err == nil {
            err = fmt.Errorf("close %s: %w", path, closeErr)
        }
    }()

    if _, err := f.Write(data); err != nil {
        return fmt.Errorf("write %s: %w", path, err)
    }
    return nil
}
```

## External Boundaries

Validate environment variables, JSON, CLI arguments, file contents, and network responses before constructing trusted domain values. Include enough context in errors to identify the operation, not secret values.
