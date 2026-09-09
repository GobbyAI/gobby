---
name: test-driven-development
description: "Required TDD loop for tasks that need red, green, refactor, final-green, and test-quality evidence."
version: "1.0.0"
category: core
internal: true
triggers: tdd, test-driven development, red green refactor
metadata:
  gobby:
    audience: all
---

# Test-Driven Development

Use this when a task requires TDD.

## Required Loop

1. Write or update the smallest meaningful test before implementation.
2. Run the exact test command and verify the expected failure.
3. Implement the smallest change that makes the new failing test pass: the
   minimal green step.
4. Run the exact test command and verify green.
5. Refactor only after green, then rerun final green validation.

## Rust Stub-First RED

Rust tests for a new API cannot reach their test bodies until the test target
compiles. Preserve test-first order with a compile-only API-shape step:

1. Write the smallest focused test before the behavior exists.
2. Add only the minimal API shape needed to compile: product types, fields,
   functions, or variants using a safe default, a behavior-free stub, or
   `todo!()`. Do not implement the behavior.
3. Run the focused test command directly in the foreground, for example:

   ```bash
   cargo nextest run -p gobby-client -E 'test(agent_status_updates_sidebar)'
   ```

   Do not wrap the command in a pipe, redirect, trailing `echo`, fallback,
   subshell, or background invocation.
4. Verify that the named test runs and RED is a test-attributed assertion or
   panic.
5. Implement the behavior, then rerun the same direct command and verify green.

Compiler failures from missing product types, unresolved imports, or unknown
fields do not count as RED. A reconstructed RED run after implementation is
also uncredited.

## Completion Evidence

The task handoff must include:

- Red evidence: exact command and test-attributed failure output from before implementation:
  assertion/panic, pytest `Failed: DID NOT RAISE`, or a test-body `*Error`/`*Exception`
  (including bare `NotImplementedError` after a stub edit).
- Green evidence: exact command and pass output summary after minimal implementation.
- Refactor/final-green evidence: exact final command and pass output summary.
- Test-quality audit evidence for supported touched test paths, or an
  unsupported-language warning plus focused repo-native validation outside Gobby.
- Test-types audit output for touched Python test paths.

When tests were added or heavily edited, run:

```bash
uv run gobby test-quality audit <paths> --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
```

Fix every reported finding regardless of severity; never raise `--min-severity`
to pass. A missing baseline is not a skip reason; the CLI falls back to treating
current supported-language issues as new.

For Python test paths, also run:

```bash
uv run gobby test-types audit <paths> --baseline .gobby/test-types-baseline.json --fail-on-new
```

A missing `.gobby/test-types-baseline.json` is not a skip reason; without it,
the CLI treats all current type errors as new.

TDD is not satisfied by writing tests after implementation, by only running a broad
suite, or by omitting the expected red failure.
