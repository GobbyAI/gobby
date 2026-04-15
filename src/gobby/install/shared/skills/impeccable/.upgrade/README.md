# Upgrade tool

One-shot refresh for the 17 impeccable steering-command references under
`../references/`. Run this when upstream
[pbakaus/impeccable](https://github.com/pbakaus/impeccable) ships updated
command bodies; it fetches, transforms, and writes them in place so you can
review the diff and commit.

```bash
uv run python src/gobby/install/shared/skills/impeccable/.upgrade/transform.py
```

The script is intentionally hand-maintained and narrowly scoped to impeccable's
upstream conventions (frontmatter shape, `{{command_prefix}}` placeholders,
`## MANDATORY PREPARATION` boilerplate). See `transform.py`'s module docstring
for the exact transformation rules. It does not touch the 9 design references
(typography, color-and-contrast, etc.) or the main `SKILL.md` dispatch block —
those are maintained by hand.

This directory is dot-prefixed (`.upgrade/`) so `SkillLoader._load_skill_files`
at `src/gobby/skills/loader.py:658` skips it entirely and the transform script
does not register as a skill resource.
