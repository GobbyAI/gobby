# JavaScript Testing

Write tests that prove runtime behavior and boundary handling, not only happy-path syntax.

## Test Runner

Use the runner already configured by the repo:

- `vitest` for Vite and many modern libraries
- `jest` when the repo already has Jest environment setup
- `node --test` for small Node packages with minimal dependencies
- Framework-local commands for Next, Remix, Astro, SvelteKit, or similar stacks

Do not introduce a second test runner for one feature unless the existing runner cannot cover the behavior.

## What To Cover

- Public functions and exported entry points.
- Boundary validation for API, file, environment, and message data.
- Error paths and rejected promises.
- Module entry points when both ESM and CommonJS are exported.
- Browser or DOM behavior with the repo's existing DOM test environment.

## Mocking

- Mock network, clock, filesystem, storage, and process boundaries.
- Prefer real module imports and public APIs for internal code.
- Keep mocks local to the test and reset them between cases.
- Use fake timers only when the test controls all timers and pending promises.

## Async Tests

- Return or await the promise under test.
- Assert rejections with the runner's rejection helper.
- Flush queued work explicitly when testing debounce, retry, or timeout behavior.
- Avoid tests that depend on real sleep durations.

## Fixtures

- Keep payload fixtures close to tests unless shared across packages.
- Include malformed payloads for validators.
- Use realistic names and fields, but keep fixtures small enough that failures are readable.

Before finishing, run the narrowest command that covers the changed package, then run broader lint or workspace checks when the change affects shared behavior.
