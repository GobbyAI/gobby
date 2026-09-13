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

## Installed verification completed

After coordinator #12967 reported the coherent v436 cutover at `faadba03d1`,
`uv run gobby sync --type skills` exited 0. It emitted informational migration
notices preserving legacy user-owned skill requirements in other tasks/sessions;
those records were not rewritten. All three Apple skills were enabled installed
filesystem rows at version 1.0.0:

| Skill | Installed ID | Entrypoint content hash |
| --- | --- | --- |
| app-store-development | 217db0f3-0596-5f7e-9bdb-ac55331905e8 | 2e719637187bc68dc09394a15122b3598b19b8c539ec99bb1608a84f9b407afc |
| safari-extension-development | 746f4469-24c8-58cd-81f1-8369ee6bed06 | 7946223136c229a4e762d88ed5fed773b97a3f281498853ef2474e14d14cf4b2 |
| app-store-review | 53be0897-4fd4-5d3a-9804-3423d77eaa92 | 20737bea181a50202083d6be09d0ffa673c5f8181fd365821d4681efa3b0d107 |

`get_skill(brief=false)` loaded each complete entrypoint. Separate
`get_skill_file` calls loaded all seven Apple references and both changed routing
references; every page was complete with null next_cursor. An exact-content check
using `uv run python` compared the three installed bodies against SkillLoader
output and all nine references against source bytes. All matched.

This check caught a stale installed Swift 1.0.0 row whose platform reference lacked
the new routing, despite sync succeeding. The normal `update_skill(name="swift")`
refresh returned `updated=true, skipped=false`; a fresh complete reference load
then matched the bundled routing. No storage/SQL mutation or override was used.

An independent [installed router check](installed-routing.md) loaded the Gobby
catalog completely across four pages, found the three Apple entries in complete
unfiltered discovery (39 entries), and successfully dispatched all three explicit
`$gobby <skill-name>` requests with selected references.

**Help scope limitation:** This session's `_active_skill_names` is explicitly `[]`.
Consequently the prescribed session-filtered help call returns zero skills, both
with `#13169` and its UUID. This is the existing allowlist behavior, not missing
installed rows; the setting and router were preserved. An isolated execution of
the real `list_skills.register` handler with the retrieved installed metadata
verified three cases: unrestricted (`None`) shows all three Apple entries, the
empty allowlist shows none, and an Apple-name allowlist shows all three. No live
session configuration or daemon database was changed for that test. This does not
claim that the three entries appeared in this restricted session's live help.

The final source checks after the UI-citation correction passed again: 63 focused
tests in 0.37 seconds; all three SkillLoader parses, all 10 content limits, all 15
exact reference calls, and all relative evidence links passed. Independent
pre-commit review covered all 21 original task files with no findings; foreign
workspace files were explicitly excluded. Original implementation is committed
as `6f0ef09d50`; this follow-up records installed evidence only.
