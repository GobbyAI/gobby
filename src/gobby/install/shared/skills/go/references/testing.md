# Go Testing Reference

Go tests should be direct, deterministic, and package-local first. Broaden scope only when the behavior crosses package or process boundaries.

## Table Tests

Use table tests for branch-heavy functions and give each case a name.

```go
func TestParseStatus(t *testing.T) {
    tests := []struct {
        name string
        input string
        want Status
        wantErr bool
    }{
        {name: "active", input: "active", want: StatusActive},
        {name: "unknown", input: "wat", wantErr: true},
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            got, err := ParseStatus(tt.input)
            if tt.wantErr {
                require.Error(t, err)
                return
            }
            require.NoError(t, err)
            assert.Equal(t, tt.want, got)
        })
    }
}
```

Use the repo's assertion style. If the repo uses only the standard library, keep new tests standard-library-only.

## Boundaries

- Use `httptest` for HTTP clients and servers.
- Use `fstest.MapFS`, `t.TempDir`, and `testdata/` for filesystem behavior.
- Use `t.Setenv` for environment-dependent code.
- Prefer real package APIs over mocks unless the dependency is slow, nondeterministic, or external.

## Concurrency

Test cancellation, timeout, and error fan-in paths. Run `go test -race` for shared state, goroutines, locks, timers, and channel ownership changes.

## Fuzzing And Golden Files

Use fuzzing for parsers and codecs that accept untrusted input. Keep golden files in `testdata/`, provide an explicit update flag, and inspect generated diffs.

## Command Selection

Start with the narrow package:

```sh
go test ./internal/parser
```

Then run the package group affected by imports. Use `./...` only when the change touches shared contracts, module files, or cross-package behavior.
