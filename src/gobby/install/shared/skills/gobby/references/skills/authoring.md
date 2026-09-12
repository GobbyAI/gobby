# Author verified skill instructions

Load when creating or changing reusable instruction behavior. Use installed
methodology through `gobby-skills`; native CLI skill tools do not record Gobby
loads. Author bundled skills under `src/gobby/install/shared/skills/<name>/`;
`.gobby/skills/` and `~/.gobby/skills/` are local authoring locations, not proof
of an installed DB row. Operator `gobby skills init` and `new` scaffold files.

Before a behavior-changing skill, add a pressure scenario under
`tests/skills/scenarios/<name>/`: record excluded-skill failure, write minimal
instructions, then record a measurable loaded-skill improvement. Include time,
authority, fatigue or shortcut pressure for discipline guidance. Pure references
can test retrieval and application. Preserve existing scenario obligations when
moving instructions; migration alone must not weaken behavior.
After the loaded run, add explicit counters for new rationalizations exposed by
the scenario, then rerun it to verify the improvement survives that pressure.

Use YAML frontmatter with valid `name` and discovery `description`. Descriptions
state triggering situations, not a workflow substitute. Keep the body reusable,
with normal workflow, invariants, exact conditional reference calls and recovery.
Validate with `gobby skills validate <path>` and the loader before installation.
Operator `gobby skills doc --format markdown` generates installed-skill
documentation; JSON/output options support inventory exports, not loaded bodies.
The guide lists supported metadata; arbitrary workflow audience strings are
invalid. Allowed-tool metadata describes requirements, not permission grants.

Bundled entrypoints and references must each fit both character and UTF-8 byte
limits from `skills.bundled_max_content_size` (default 15000). Split by semantic
topic with exact loading conditions; normally activate at most three references.
Scripts, assets, notices and non-bundled files are outside this authoring ceiling.
The authoring tests enforce it; sync warns without truncating instruction text.

Run focused scenario, parser, contract and link checks against isolated state.
Verify discovery after the authorized sync/cutover, preserve artifacts and
validators, and cite executed evidence. Do not claim a successful scenario when
only parser validation ran. Retained standalone methodologies should link here
for Gobby-specific authoring and to other capability references for procedures.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#behavioral-skill-validation).

_Last verified: 2026-09-12_
