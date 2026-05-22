---
name: test-driven-development
description: "Required TDD loop for tasks that need red, green, refactor, final-green, and test-quality evidence."
version: "1.0.0"
category: core
triggers: tdd, test-driven development, red green refactor
metadata:
  gobby:
    audience: agent
    depth: 1
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

## Completion Evidence

The task handoff must include:

- Red evidence: exact command and failure output summary from before implementation.
- Green evidence: exact command and pass output summary after minimal implementation.
- Refactor/final-green evidence: exact final command and pass output summary.
- Test-quality audit evidence for touched test paths, or a specific reason it was not applicable.

When tests were added or heavily edited, run:

```bash
uv run gobby test-quality audit <paths> --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high
```

TDD is not satisfied by writing tests after implementation, by only running a broad
suite, or by omitting the expected red failure.
