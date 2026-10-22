---
name: test-driven-development
description: "Tests-first development with focused red, green, refactor, and reviewer reruns."
version: "1.1.0"
category: core
internal: true
triggers: tdd, test-driven development, red green refactor
metadata:
  gobby:
    audience: all
---

# Test-Driven Development

Use this when a task requires TDD. The developer and reviewer judge the test
sequence from the work and its commits.

## Required Loop

1. Write the smallest meaningful test for the intended behavior before implementing it.
2. Run that test and inspect its expected assertion failure. Fix collection,
   environment, or compilation failures before treating the run as RED.
3. Commit the test when useful to make the tests-first sequence reviewable.
4. Implement the smallest complete change that makes the failing test pass.
5. Run the focused tests and inspect GREEN.
6. Refactor, then rerun the relevant validation.

Named acceptance tests should exercise observable behavior and meaningful failure
cases. Keep unchanged regression coverage and add coverage for the changed contract.
Do not manufacture failures or rewrite historical results.

## New APIs and Rust

When a test cannot compile without a new API, first add the minimal behavior-free
shape needed to reach the test body. A safe default, stub, or `todo!()` can prepare
that run. Inspect the resulting assertion or panic before implementing the behavior.

Run focused commands directly, for example:

```bash
cargo nextest run -p gobby-client -E 'test(agent_status_updates_sidebar)'
```

## Review and Handoff

Include the tests-first commit sequence, the commands used, the expected failing
behavior, and the final passing results. The reviewer inspects test quality and
reruns relevant tests independently. Fix review findings and rerun affected checks.

For added or substantially changed tests, run the repository's quality checks:

```bash
uv run gobby test-quality audit <paths> --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
uv run gobby test-types audit <python-test-paths> --baseline .gobby/test-types-baseline.json --fail-on-new
```

Fix reported issues. For unsupported languages, use focused native validation.
A missing baseline means current findings are new; it does not waive the check.
