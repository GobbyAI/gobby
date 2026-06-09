# Testing - Reference

Source note: adapted for Gobby from the CUBETIQ TypeScript testing reference and cross-checked against SkillsMP review-oriented TypeScript guidance.

## Test Ownership

Use the repo's existing stack. TypeScript tests usually fall into three groups:

| Need | Typical tool |
| --- | --- |
| Runtime unit behavior | `vitest`, `jest`, Node test runner, framework runner |
| Public type contracts | `expectTypeOf`, `tsd`, compile-only `.test-d.ts` files |
| Browser/device evidence | Playwright, Cypress, framework e2e, mobile tools |

Do not route ordinary TypeScript code-level checks through a generic testing skill. Use framework or platform skills only when the behavior depends on that framework or runtime.

## Runtime Tests

Test public behavior and boundaries:

```ts
import { describe, expect, it } from "vitest";

describe("parseUserId", () => {
  it("brands valid ids", () => {
    expect(parseUserId("usr_123")).toBe("usr_123");
  });

  it("rejects invalid ids", () => {
    expect(() => parseUserId("post_123")).toThrow(TypeError);
  });
});
```

Prefer focused cases over snapshotting large objects. Use table tests when the cases share one assertion shape.

## Type Tests

For Vitest projects:

```ts
import { expectTypeOf, it } from "vitest";

it("preserves event literals", () => {
  const events = defineEvents(["open", "close"]);
  expectTypeOf(events).toEqualTypeOf<readonly ["open", "close"]>();
});
```

For public packages, use `tsd` or compile-only assertion files:

```ts
import { expectType } from "tsd";

expectType<UserId>(parseUserId("usr_123"));
```

Negative type tests should use `@ts-expect-error`:

```ts
// @ts-expect-error PostId cannot be used where UserId is required.
fetchUser(parsePostId("pst_123"));
```

`@ts-expect-error` is part of the assertion. It fails if the compiler no longer reports an error.

## Boundary Tests

When runtime validation protects static types, test both:

```ts
it("rejects malformed API users", () => {
  expect(() => ApiUser.parse({ id: "bad", email: "x" })).toThrow();
});

it("returns a branded user id", () => {
  const user = ApiUser.parse({ id: "usr_123", email: "a@example.com", name: "Ada" });
  expectTypeOf(user.id).toEqualTypeOf<UserId>();
});
```

This catches drift between schema behavior and TypeScript declarations.

## Async Tests

- Await every promise under test.
- Use fake timers or controlled promises instead of sleeping.
- Test cancellation, timeout, retry exhaustion, and partial failure paths.
- Assert rejected promises with `await expect(promise).rejects`.

```ts
await expect(fetchWithTimeout(url, { timeoutMs: 1 })).rejects.toThrow(TimeoutError);
```

## Mocking

Mock external boundaries, not internals:

- HTTP clients and transport adapters
- filesystem and clock wrappers
- browser APIs behind thin adapters
- database or queue clients at repository boundaries

Avoid mocking the helper being tested. If the only way to test code is heavy internal mocking, split the boundary or simplify the design.

## Validation Commands

Use the repo's scripts first. Common focused commands:

```bash
pnpm test --filter <package>
pnpm tsc -p packages/<name>/tsconfig.json --noEmit
pnpm vitest run path/to/file.test.ts
pnpm tsd
```

Match the package manager and workspace tool already present in the repo.
