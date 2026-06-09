# Types - Reference

Source note: adapted for Gobby from the CUBETIQ `typescript-best-practices` SkillsMP source and cross-checked against SkillsMP review-oriented TypeScript guidance.

## Unknown First At Boundaries

External input starts as `unknown` until validated:

```ts
function parsePayload(input: unknown): Payload {
  const parsed = PayloadSchema.parse(input);
  return parsed;
}
```

Avoid `as Payload` on API responses, file reads, environment variables, `postMessage`, local storage, and user input. Assertions are acceptable only after a validator or a narrow guard proves the shape.

## Discriminated Unions

Use a literal discriminant for state with variant-specific fields:

```ts
type LoadState<T> =
  | { status: "idle" }
  | { status: "loading"; requestId: string }
  | { status: "success"; data: T }
  | { status: "failure"; error: AppError };

function render<T>(state: LoadState<T>): string {
  switch (state.status) {
    case "idle":
      return "Ready";
    case "loading":
      return state.requestId;
    case "success":
      return String(state.data);
    case "failure":
      return state.error.message;
    default: {
      const unreachable: never = state;
      return unreachable;
    }
  }
}
```

Do not model this with one interface full of optional fields. Optional fields move invalid states from compile time to runtime.

## Branded Domain Values

Brand primitives that carry domain meaning:

```ts
declare const UserIdBrand: unique symbol;

export type UserId = string & { readonly [UserIdBrand]: "UserId" };

export function parseUserId(value: string): UserId {
  if (!value.startsWith("usr_")) {
    throw new TypeError(`Invalid user id: ${value}`);
  }
  return value as UserId;
}
```

Brand at the boundary, not at every call site. Keep the constructor small and tested. Use brands for IDs, tokens, emails, URLs, slugs, and units that are easy to mix up.

## `satisfies`

Use `satisfies` for config and lookup objects:

```ts
type RouteConfig = Record<string, { method: "GET" | "POST"; auth: boolean }>;

const routes = {
  users: { method: "GET", auth: true },
  login: { method: "POST", auth: false },
} satisfies RouteConfig;
```

This checks the shape while preserving literal information for downstream inference. Prefer it over `as RouteConfig`.

## Type Guards And Assertions

Use guards for reusable narrowing:

```ts
function isDefined<T>(value: T | null | undefined): value is T {
  return value != null;
}

function assertError(value: unknown): asserts value is Error {
  if (!(value instanceof Error)) {
    throw new TypeError("Expected Error");
  }
}
```

Capture narrowed values in `const` before passing them into callbacks. TypeScript intentionally stops trusting mutable outer variables inside closures.

## Advanced Type Tools

Use conditional, mapped, and template-literal types in library or shared API surfaces where they remove real duplication:

```ts
type ValueOf<T> = T[keyof T];

type EventHandlers<T> = {
  [K in keyof T as `on${Capitalize<string & K>}`]: (value: T[K]) => void;
};
```

Name intermediate types so compiler errors and editor hovers stay readable. Avoid deeply recursive types in application code unless the repo already depends on that level of type programming.

## Const Type Parameters

Use `const` type parameters when literal inference is the API:

```ts
function defineEvents<const Events extends readonly string[]>(events: Events): Events {
  return events;
}

const events = defineEvents(["open", "close"]);
```

This preserves tuple literals without forcing callers to write `as const` everywhere.

## Utility Type Rules

- Use built-ins (`Pick`, `Omit`, `Record`, `Exclude`, `Extract`, `Awaited`) when they stay readable.
- Create named domain types once a shape appears in public APIs or multiple modules.
- Prefer `Record<KeyUnion, Value>` for complete maps over `Partial<Record<...>>` when every key is required.
- Use `Readonly` at API boundaries to communicate immutability; do not use it as a substitute for copying mutable values.
- Avoid `Record<string, any>`; use `Record<string, unknown>` or a specific value type.

## Public Type Surface

Package entrypoints should export public types deliberately:

```ts
export type { ClientConfig, ClientResult } from "./types";
export { createClient } from "./client";
```

Do not re-export private helpers just because a barrel file is convenient. Public types become compatibility commitments.
