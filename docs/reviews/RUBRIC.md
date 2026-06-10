# Review Rubric — Python (src/gobby)

> **Starting point, not a straitjacket.** This encodes the floor every review
> must clear. If a subsystem demands deeper or different scrutiny (a parser's
> fuzzing surface, a scheduler's clock math, a security boundary), expand on
> this — add axes, go deeper. Do not contract below it.

## Mission

Find **correctness bugs and contract violations first**, then maintainability
risks. Not style. A reviewer who returns ten nits and misses the race condition
has failed.

## Severity

| Severity | Bar |
|---|---|
| **Blocker** | Wrong behavior, data loss/corruption, security hole, crash, contract violated, resource leak under normal use. Ship-stopping. |
| **Important** | Real bug in an edge case, missing error handling, concurrency hazard, silent failure, contract drift, absent test for load-bearing behavior. Should fix before it bites. |
| **Nit** | Maintainability, naming, dead code, minor duplication, a clarifying comment. Backlog-grade. |

## Evidence discipline (non-negotiable)

- **Cite the exact `file:line` or drop the finding.** No "somewhere in X handles
  this wrong." If you can't point at it, you can't report it.
- **Mark uncertainty; never inflate it away.** Below ~70% confidence, label the
  finding `confidence: low` and say what would confirm it. Do not delete a real
  doubt and do not dress a hunch as a certainty.
- **No invented findings.** A plausible-sounding bug you didn't verify is noise.
  Triage trusts this file; poison it and the whole epic degrades.

## Ground every finding in the repo's contracts

These are Gobby's own rules (see `CLAUDE.md`). A violation is at least Important:

- **DB:** `$N` placeholders, never `?`; writes inside the `self.db.transaction()`
  boundary; related fields written atomically (e.g. worktree/clone path+ID pairs).
- **MCP proxy:** progressive discovery only — never call one step through another
  (no `call_tool` invoking `get_tool_schema`).
- **Errors:** specific exceptions, never bare `except:` / `except Exception: pass`.
- **Types:** full type hints on every function signature.
- **Async:** `async` for I/O-bound work; no blocking calls on the event loop;
  no un-awaited coroutines; cancellation/cleanup handled.
- **Dispatch determinism:** dispatcher routes manifest state only — no prompting,
  no LLM calls in `dispatch/`. Mutex acquired before side effects, released in
  `finally`.
- **Templates vs DB:** the DB is the source of truth for active rules/workflows;
  `install/shared/` files are templates synced on startup. Flag logic that treats
  the YAML as authoritative at runtime.
- **Monolith cap:** non-test `.py` over **1,000 lines** is a finding (Nit unless it
  hides real complexity). Note it; do not refactor here.

## What to actively hunt

Correctness · concurrency & async races · resource/connection leaks · unhandled
errors & silent failures · injection / secret handling / authz gaps · API &
schema contract drift · off-by-one and boundary math · dead/unreachable code ·
obvious perf cliffs on hot paths · **test gaps** (does a test exist, and does it
assert real behavior rather than mocking the assertion away?).

## Out of scope (don't report)

Formatting, import order, line length, quote style — anything `ruff` or `mypy`
already enforces. Pure taste. If a tool would catch it, it's not your job.

## Method

- Use **`gcode`** for symbol/graph lookups, not grep/rg/sed. Read **whole files**
  before judging cross-file behavior — most real bugs live in the seams.
- Trace the contract, not just the function: who calls this, what do they assume,
  what breaks if the assumption is wrong.
- Check the tests that *should* cover the code you doubt.

## Output

Write findings to `docs/reviews/<area>.md` using `TEMPLATE.md`. **If the area is
clean, say so explicitly** — write the file with a "No findings" line and your
confidence. An empty/clean review is still a committed deliverable.
