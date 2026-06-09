# Error Handling - Reference

Source note: adapted for Gobby from the CUBETIQ TypeScript patterns source and cross-checked against SkillsMP JavaScript strict/error-handling guidance.

## Separate Expected Failures From Exceptions

Use typed results for expected business or validation failures:

```ts
type Result<T, E> =
  | { ok: true; value: T }
  | { ok: false; error: E };

type LookupError =
  | { kind: "not_found"; id: UserId }
  | { kind: "permission_denied"; id: UserId };
```

Throw for exceptional failures: corrupted invariants, programmer errors, unavailable dependencies, or unexpected third-party behavior. Catch at boundaries that can log, retry, convert, or display the failure.

## Catch Unknown

With strict settings, caught values should be treated as `unknown`:

```ts
function toError(value: unknown): Error {
  if (value instanceof Error) {
    return value;
  }
  return new Error(String(value));
}

try {
  await syncUser(userId);
} catch (cause: unknown) {
  throw new Error(`Failed to sync user ${userId}`, { cause: toError(cause) });
}
```

Never assume caught values are `Error`; JavaScript can throw strings, objects, `null`, or numbers.

## Boundary Validation

Validate untrusted input once, then pass typed values inward:

```ts
const ApiUser = z.object({
  id: z.string().transform(parseUserId),
  email: z.string().email(),
  name: z.string(),
});

type ApiUser = z.infer<typeof ApiUser>;

async function fetchUser(id: UserId): Promise<ApiUser> {
  const response = await fetch(`/users/${id}`);
  const body: unknown = await response.json();
  return ApiUser.parse(body);
}
```

Use the repo's validator (`zod`, `valibot`, `arktype`, `io-ts`, custom guards). The important part is that runtime and static types are tied together.

## Error Classes

Use error classes for reusable exceptional categories:

```ts
class ExternalServiceError extends Error {
  readonly service: string;

  constructor(
    message: string,
    service: string,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ExternalServiceError";
    this.service = service;
  }
}
```

Include machine-readable fields when callers need to branch. Avoid parsing error messages.

## Async Error Context

Attach context at the layer that has it:

```ts
async function loadConfig(path: string): Promise<Config> {
  try {
    return ConfigSchema.parse(JSON.parse(await readFile(path, "utf8")));
  } catch (cause: unknown) {
    throw new Error(`Unable to load config from ${path}`, { cause: toError(cause) });
  }
}
```

Do not catch merely to log and rethrow unless the boundary owns logging. Duplicate logs make production incidents harder to read.

## Exhaustive Error Handling

For result-style errors, use discriminants and `never`:

```ts
function messageFor(error: LookupError): string {
  switch (error.kind) {
    case "not_found":
      return `Missing user ${error.id}`;
    case "permission_denied":
      return `Access denied for ${error.id}`;
    default: {
      const unreachable: never = error;
      return unreachable;
    }
  }
}
```

This forces new error variants to be handled everywhere the switch is used.

## Suppression Rules

- Use `@ts-expect-error` only for deliberate negative type assertions or temporary migrations.
- Add a short reason after every suppression.
- Do not use `@ts-ignore` in new code.
- Do not convert `unknown` to `any` to bypass a boundary. Validate or narrow it.

## Logging

Log structured context at boundary layers:

```ts
logger.error("user_sync_failed", {
  userId,
  cause: error.message,
});
```

Never log secrets, full tokens, credentials, raw cookies, or unredacted personal data.
