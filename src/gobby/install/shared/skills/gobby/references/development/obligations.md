# Developer obligations

Load before implementing a developer-agent task or preparing validation evidence.

## Preflight

1. Create or claim the deliverable through task MCP before edits. An existing
   claimed root can own its planned children; preserve the established ownership
   model. Initialize the provider's native implementation tracker when available.
2. Read repository and directory instructions, supplied research and specification.
   Search memory before unfamiliar code. Resolve named symbols and callers with
   gcode; approximate lines are hints. Inspect relevant diffs and recent commits,
   preserving other sessions' work.
3. Confirm capability ownership, dependency direction, state authority, public
   surfaces and test placement. Rediscover only stale or missing evidence. Load
   `repository-maintenance` for package creation/moves, cross-package dependencies,
   new shared abstractions or ownership changes; `decompose-monolith` before production decomposition. Hand-maintained
   production files must stay below 1,000 lines, including after the change.
4. State a test judgment before editing: unit, integration, CLI/API, browser/UI,
   migration/storage or a documented manual check. Load `test-driven-development`
   for `tdd:required`, an explicit skill requirement or TDD acceptance evidence.

## Verify in isolated state

Use `uv` for Python. Focused backend example:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/<matching-file>.py -q
```

The database must be the isolated test hub, and daemon fixtures need temporary
home/state and ports. Never exercise fabricated mutation examples against the
user's daemon. Run focused Rust packages or test filters; avoid bare/workspace
`cargo test`. Never run full pytest without an explicit user request.

Behavior changes require appropriate tests. Pure documentation/mechanical
metadata may use direct validation; explain the gap. After adding or heavily
editing supported-language tests, run:

```bash
uv run gobby test-quality audit <touched-tests> --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
```

Fix every finding, including low/medium severity. Never raise the threshold to
pass. A missing baseline counts current findings as new. Record unsupported
language warnings with focused native validation. For Python test edits also run:

```bash
uv run gobby test-types audit <touched-tests> --baseline .gobby/test-types-baseline.json --fail-on-new
uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json
```

Close credits one type-audit invocation whose explicit targets lexically cover
every changed Python test. A file target covers itself, a directory target covers
its descendants, and `tests/` is the whole-tree fallback. A rename needs both the
source and the destination path; a deleted test cannot be its own target, so audit
its parent directory. Coverage is judged per invocation and never unioned across
runs, so auditing touched tests one file per call leaves every run short. Besides
that baseline and `--fail-on-new`, only the output-only flags `--format`,
`--output` and `--min-severity` keep credit; any other flag voids it.

Run the suppression ratchet after Python or test edits. Fix types with explicit
types, casts at deliberate invalid-input boundaries and typed seams; add no
`type: ignore` or `noqa`. A missing suppression baseline is an error. Its
`--write-baseline` only accepts strict debt reduction with no new/changed sites.
Test-type baseline regeneration uses `--write-baseline <same-baseline>` alongside
the passing ratchet. `--allow-failing-baseline` needs explicit reviewed acceptance,
not routine failure recovery. Report-only exit zero is not proof of no findings.

## Completion and recovery

Fix findings within the owned task. Hand foreign active work to its owner with
command, diagnostics, paths and impact; use the task found-work ladder for genuine
decision/planning/clean-window blockers. Operational coordination stays part of
the fix. Read [task closing](../tasks/closing.md) for exact gates.

Finish all edits before final validation, follow yielded commands to their exit,
then commit only owned paths and close through task MCP. Every completion/review
handoff gives exact commands, results, coverage rationale and remaining gaps.
Use the session handoff procedure before context reset; a menu load does not
satisfy an operation reference requirement.

For UI work load `impeccable` and read `.impeccable.md` before design edits. Follow
the [activity-tab recipe](../../../../../../../../docs/guides/one-surface-tab-recipe.md)
and scoped browser validation. Native socket/platform requirements are in
[native components](native-components.md). Human details:
[testing](../../../../../../../../docs/guides/testing.md) and
[test quality](../../../../../../../../docs/guides/test-quality.md).
