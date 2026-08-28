---
name: writing-skills
description: Use when creating, editing, or validating Gobby skills, especially when a skill must change agent behavior or become discoverable through gobby-skills.
version: "1.0.0"
category: authoring
triggers:
  - create skill
  - edit skill
  - writing skills
  - skill authoring
  - validate skill
metadata:
  gobby:
    audience: all
    depth: 0
---

# Writing Skills

Writing skills is test-driven development for process documentation. A skill is
production behavior: it should change what future agents do under pressure.

Core rule: no skill without a failing scenario first.

## Gobby Skill Surfaces

Gobby skills are managed through the `gobby-skills` MCP server. Use
`search_skills` and `get_skill`; do not rely on native CLI skill tools.

Bundled skills live in `src/gobby/install/shared/skills/<skill-name>/SKILL.md`.
Project skills live in `.gobby/skills/`. User skills live in `~/.gobby/skills/`.

## Red-Green-Refactor

Before writing or editing a behavior-changing skill:

1. RED: Add or update a pressure scenario under `tests/skills/scenarios/<skill-name>/`.
2. Baseline: record behavior with the skill excluded. The agent should miss the rule,
   shortcut the process, or produce the wrong output.
3. GREEN: Write the minimal `SKILL.md` content that blocks that failure.
4. Loaded run: record behavior with the skill loaded. It must show a measurable delta.
5. REFACTOR: Add explicit counters for any new rationalization the scenario exposes.

Run scenario tests with:

```bash
uv run pytest tests/skills/ -m skill_tdd
```

For pure reference skills, the scenario can verify retrieval and application.
For discipline skills, combine pressure: time, sunk cost, authority, fatigue, or
temptation to skip verification.

## Frontmatter

Use valid YAML frontmatter:

```yaml
---
name: skill-name
description: Use when <triggering situation, symptoms, or task>
version: "1.0.0"
category: authoring
metadata:
  gobby:
    audience: all
---
```

The description is only a trigger. Do not summarize the workflow there; agents
may follow the description instead of loading the full skill.

Good:

```yaml
description: Use when creating, editing, or validating Gobby skills.
```

Bad:

```yaml
description: Use to run red-green-refactor, write scenarios, then deploy skills.
```

## Skill Body

Keep the body reusable:

- Define the judgment or technique, not the story of one session.
- Put searchable symptoms and tool names near the top.
- Prefer one sharp example over many generic examples.
- Move heavy references, scripts, or templates into `references/`, `scripts/`, or
  `assets/` only when inline text would become bulky.
- Cross-reference other skills by name, such as
`REQUIRED SKILL: tasks`.

## Bundled Content Ceiling

Gobby-owned bundled `SKILL.md` and `references/**` files must satisfy both checks against the
live `skills.bundled_max_content_size` setting (default `15000`):

```python
len(text) <= configured_limit
len(text.encode("utf-8")) <= configured_limit
```

Project, user, hub, script, asset, and notice files are outside this authoring ceiling.
`skill_tdd` fails every bundled violation with path, character count, byte count, limit, and
decomposition guidance. Bundled sync stays permissive and emits the same guidance as a warning.

When a bundled instruction file exceeds the resolved limit:

1. Keep `SKILL.md` as purpose, common path, invariants, and topic index.
2. Move conditional detail to topic-named references.
3. Keep each reference within the same limit.
4. State the exact condition and
   `get_skill_file(name="<skill>", path="references/<topic>.md")` call for every reference.
5. Split semantically, using names such as `recovery.md` or `verification.md` instead of
   numbered parts.
6. Keep normal workflows within a three-reference activation budget.
7. Preserve expected artifacts, validators, and recovery behavior.

## Common Failures

| Failure | Fix |
| --- | --- |
| Skill says what to do but has no pressure scenario | Add a `tests/skills/scenarios/<skill-name>/` case first |
| Description summarizes process | Rewrite it as a trigger only |
| Skill is a one-off narrative | Extract the reusable technique or do not create a skill |
| Reference is too long for the main file | Move it to `references/` and name when to open it |
| Bundled file exceeds the resolved ceiling | Split by topic and add exact conditional routing |
| Scenario shows no behavioral delta | Tighten the skill or delete the unnecessary guidance |

## Deployment Check

Before finishing:

1. `SkillLoader().load_skill(...)` can parse the skill.
2. The skill is discoverable after daemon sync through `gobby-skills`.
3. `uv run pytest tests/skills/ -m skill_tdd` passes for its scenario.
4. Bundled authoring validation passes at the resolved content ceiling.
5. Any focused skill contract tests pass.
6. Completion claims cite fresh verification evidence, including expected artifacts,
   validators, and recovery behavior.
