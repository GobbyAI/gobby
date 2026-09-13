# Validation record

Task #22302, 2026-09-13. Validation is scoped to the three new Apple skills and
their Gobby/Swift routing. No app implementation or enforcement hooks changed.

## Source checks executed

- `uv run gobby skills validate src/gobby/install/shared/skills/app-store-development`
- `uv run gobby skills validate src/gobby/install/shared/skills/safari-extension-development`
- `uv run gobby skills validate src/gobby/install/shared/skills/app-store-review`

All three exited 0 with valid names/frontmatter. Gobby's own parser is used because
these bundles carry Gobby metadata and are not Codex-only skills.

`SkillLoader().load_skill(...)` was executed with `uv run python` for each bundle.
Names matched directories; detected reference paths exactly matched the files:
development 4, Safari 2, review 1.

A `uv run python` path/size check walked all 10 new instruction Markdown files,
asserted character and UTF-8 byte counts at most 15,000, resolved all 15 exact
`get_skill_file(name=..., path=...)` calls against the bundled library, and checked
relative `references/` links. Passed. External source retrieval and limitations
are recorded in [sources](sources.md).

Executed:

```sh
DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_bundled_skill_content_size.py tests/skills/test_gobby_skill_router.py tests/skills/test_capability_routing.py tests/skills/test_language_skill_editorial.py -q
```

Result: 63 passed in 0.31 seconds, exit 0. This covers repository-wide bundled
instruction limits and existing routing/editorial contracts without the full suite.
`git diff --check` also passed. Parser success is not behavioral evidence.

## Installation coordination

Initial `uv run gobby sync --type skills` exited 1: installed/embedded schema v435
did not match checkout-expected v436. The uncommitted schema/title work belonged
to active task #22207. Its owner received the failing command, diagnostics, paths
and impact through project and targeted Gobby messages. Those foreign files were
preserved. Subsequent installed evidence is recorded below when completed.
