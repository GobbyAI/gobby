# JavaScript Error Handling

Treat JavaScript error handling as part of the public contract, not cleanup after the main path works.

## Throwing

- Throw `Error` instances or subclasses, not strings or plain objects.
- Use `TypeError`, `RangeError`, and domain-specific classes when callers can act on the distinction.
- Include actionable context in messages without leaking secrets.
- Add `cause` when wrapping errors at boundaries.

```js
try {
  return await loadConfig(path);
} catch (error) {
  throw new Error(`Failed to load config from ${path}`, { cause: error });
}
```

## Expected Failures

Use explicit result objects for expected domain outcomes:

```js
export function parseToken(value) {
  if (typeof value !== "string" || value.length === 0) {
    return { ok: false, error: "missing_token" };
  }
  return { ok: true, token: value };
}
```

Reserve thrown errors for exceptional failures, programming mistakes, or process/request boundaries that already translate exceptions.

## Fetch and External Data

- Check `response.ok` and status codes before parsing.
- Handle invalid JSON separately from valid error responses.
- Validate parsed data before returning it.
- Preserve response metadata that helps debugging, such as status and request id.

## Catch Blocks

- Catch narrowly, at boundaries that can add context or recover.
- Never leave empty catches. If ignoring is intentional, state why in a short comment.
- Re-throw unknown failures after cleanup.
- Keep cleanup in `finally` when it must run for success and failure.

## Process Boundaries

- Register unhandled rejection and uncaught exception behavior intentionally in CLIs and daemons.
- Log structured context at top-level boundaries, not inside tight loops.
- Do not log raw tokens, secrets, cookies, authorization headers, or full user payloads.
