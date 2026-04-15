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
is derived from the upstream repo's `source/skills/impeccable/` directory. Only
the main `impeccable` skill and its `reference/` files are included. The 18
companion "steering commands" (adapt, animate, audit, bolder, clarify, colorize,
critique, delight, distill, harden, layout, optimize, overdrive, polish,
quieter, shape, typeset) and maintenance scripts are not vendored.

Modifications from upstream:
- Frontmatter rewritten to match the Gobby shared-skill format (`category`,
  `triggers`, `metadata.gobby.audience`).
- `{{command_prefix}}`, `{{model}}`, `{{config_file}}`, `{{ask_instruction}}`,
  and `{{scripts_path}}` placeholders substituted with concrete language
  appropriate for a Gobby runtime.
- `<post-update-cleanup>` block removed (no scripts are shipped).
- `references/craft.md` Step 1 rewritten to interview the user directly in
  place of calling the absent `shape` command.
- `reference/` directory renamed to `references/` (plural) to match the
  Gobby shared-skill convention used by `SkillLoader._scan_subdirectory`.

## Anthropic frontend-design Skill

The `impeccable` skill in the upstream project builds on Anthropic's original
frontend-design skill.

- Original work: https://github.com/anthropics/skills/tree/main/skills/frontend-design
- Original license: Apache License 2.0
- Copyright: 2025 Anthropic, PBC

The upstream project extends the original with domain-specific reference files,
steering commands, and expanded patterns and anti-patterns. See the upstream
repo's `NOTICE.md` for the full attribution chain.
