# Notice

Impeccable
Copyright 2025-2026 Paul Bakaus

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
this file except in compliance with the License. You may obtain a copy of the
License at:

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.

## Upstream

- Source: https://github.com/pbakaus/impeccable
- License: Apache License 2.0
- Copyright: 2025-2026 Paul Bakaus

The `impeccable` skill vendored at `src/gobby/install/shared/skills/impeccable/`
is derived from the upstream repo's `source/skills/impeccable/` directory plus
all 17 companion "steering commands" from `source/skills/<cmd>/` (`adapt`,
`animate`, `audit`, `bolder`, `clarify`, `colorize`, `critique`, `delight`,
`distill`, `harden`, `layout`, `optimize`, `overdrive`, `polish`, `quieter`,
`shape`, `typeset`). The upstream bundle treats each steering command as its
own slash-command skill; Gobby ships them as **reference files** under
`references/<cmd>.md` and the main `impeccable` `SKILL.md` acts as a router
that dispatches to them via `get_skill_file(name="impeccable", path="...")` on
the `gobby-skills` MCP server.

No upstream maintenance scripts are vendored — `scripts/cleanup-deprecated.mjs`
is an upgrade-migration tool for end-user installs and is irrelevant for a
fresh vendor install.

## Modifications from upstream

Main skill (`SKILL.md`):
- Frontmatter rewritten to match the Gobby shared-skill format (`category:
  frontend`, `triggers`, `metadata.gobby.audience: all`,
  `metadata.gobby.format_overrides.autonomous: full`).
- `<post-update-cleanup>` block removed.
- `{{command_prefix}}`, `{{model}}`, `{{config_file}}`, `{{ask_instruction}}`,
  and `{{scripts_path}}` placeholders substituted with concrete language
  appropriate for a Gobby runtime.
- `## Sub-command Dispatch` section added directly after the Context Gathering
  Protocol. Dispatches inline modes (`craft`, `teach`, `extract`) to the
  existing sections, and steering-command arguments to
  `get_skill_file(name="impeccable", path="references/<cmd>.md")`.
- `reference/` directory renamed to `references/` (plural) to match the Gobby
  shared-skill convention used by `SkillLoader._scan_subdirectory`.

Steering-command references (`references/<cmd>.md`, 17 files):
- Upstream frontmatter block stripped entirely. Reference files under
  `references/` do not need their own frontmatter — the loader classifies them
  by directory (`SkillLoader._classify_file`).
- Upstream's `## MANDATORY PREPARATION` boilerplate (which instructed the LLM
  to invoke the main `impeccable` skill before proceeding) collapsed to a
  single blockquote: *"You are continuing a session under the `impeccable`
  skill; the design-context protocol and anti-pattern rules already apply."*
  Any `Additionally gather: ...` clause from the upstream block was preserved
  inline.
- `critique.md` uses an upstream `### Step 1: Preparation` subsection instead
  of the standard block; the preamble note was inserted there with the extra
  clause preserved.
- `harden.md` and `optimize.md` have no upstream preparation section; the
  preamble note was prepended above the first heading.
- Cross-references to other steering commands (`{{command_prefix}}polish`,
  `{{command_prefix}}audit`, etc.):
  - Inside existing backtick code spans → collapsed to the bare command name
    (e.g., `` `polish` ``) to avoid breaking markdown rendering.
  - In prose context → expanded to `the \`<cmd>\` steering command (load via
    \`get_skill_file(name="impeccable", path="references/<cmd>.md")\` on
    \`gobby-skills\`)`.
- `{{available_commands}}` substituted with the full backtick-quoted list of
  17 steering commands so `audit` and `critique` render their recommendation
  sections correctly.
- `{{command_prefix}}command-name` (an upstream documentation placeholder
  meaning *"fill in the actual command here"*) substituted with `` `<cmd>` ``.

Craft-mode references (`references/craft.md`):
- Step 1 updated to call `get_skill_file(name="impeccable",
  path="references/shape.md")` via `gobby-skills` instead of the upstream
  `{{command_prefix}}shape` slash command.

## Anthropic frontend-design Skill

The `impeccable` skill in the upstream project builds on Anthropic's original
frontend-design skill.

- Original work: https://github.com/anthropics/skills/tree/main/skills/frontend-design
- Original license: Apache License 2.0
- Copyright: 2025 Anthropic, PBC

The upstream project extends the original with domain-specific reference files,
steering commands, and expanded patterns and anti-patterns. See the upstream
repo's `NOTICE.md` for the full attribution chain.
