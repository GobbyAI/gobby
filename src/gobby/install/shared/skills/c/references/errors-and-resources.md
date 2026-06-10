# C Errors And Resources

Error handling in C is part of the API. Keep it consistent with the surrounding
module and make cleanup behavior boring.

## Error Conventions

- Follow the local convention: zero/nonzero, negative errno, enum status,
  bool-plus-output, pointer-or-null, or project result struct.
- Preserve errno or platform error codes before cleanup code can overwrite them.
- Return the most useful error at the layer that still has dependency context.
- Do not collapse allocation failure, parse failure, invalid input, and I/O
  failure into one vague status if callers need to react differently.

## Output Parameters

- Validate pointers before writing through them.
- Leave output parameters untouched on failure unless the API documents defaults.
- When partial output is valid, document which fields are initialized.
- Prefer local temporaries and commit to outputs only after success.

## File, Socket, And Process Resources

- Close file descriptors, streams, sockets, handles, locks, and child processes
  on every path.
- Check partial reads and writes. Loop intentionally or propagate short I/O.
- Make blocking, timeout, cancellation, signal, and EINTR behavior explicit.
- Keep platform-specific resource APIs behind small wrappers when portability
  matters.

## Logging And Diagnostics

- Log where the operation context is available, not at every helper layer.
- Include operation, path, endpoint, or subsystem context without leaking secrets.
- Preserve causal error information for callers and tests.
- Keep assertions for programmer invariants; return errors for runtime input and
  environmental failures.

## Cleanup Patterns

Prefer a structured cleanup path:

```c
int rc = 0;
FILE *file = fopen(path, "rb");
char *buffer = NULL;

if (file == NULL) {
    rc = -errno;
    goto out;
}

buffer = malloc(size);
if (buffer == NULL) {
    rc = -ENOMEM;
    goto out;
}

out:
free(buffer);
if (file != NULL) {
    fclose(file);
}
return rc;
```

Adapt the convention to the project instead of copying this shape blindly.
