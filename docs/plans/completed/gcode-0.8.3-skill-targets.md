# gcode 0.8.3 Skill Target Refresh

## Context

`gcode init` previously installed the bundled `gcode` skill only when selected
AI CLI directories were already present. The 0.8.3 release switches to a fixed
project-local target list so non-Gobby-managed projects get the same skill
wiring regardless of which CLI directories existed before init.

## Completed Changes

- Added explicit skill targets for Claude Code, Codex, Droid, Grok, Qwen,
  Gemini CLI (deprecated compatibility), and Antigravity CLI.
- Preserved Gobby-managed behavior: when project identity resolves through
  `.gobby/project.json`, `gcode init` skips project-local skill installation.
- Kept Claude Code plugin installation as `.claude-plugin/plugin.json` plus
  `skills/gcode/SKILL.md`.
- Marked Gemini CLI deprecated in code comments, output labels, and docs while
  continuing to install `.gemini/skills/gcode/SKILL.md`.
- Added focused installer tests covering all target paths, Claude plugin
  output, deprecated Gemini installation, and preservation of existing CLI
  files.

## Out Of Scope

This release does not change `ghook` or `gloc` behavior. Lifecycle adapters for
`agy` and `grok` remain separate `gobby-hooks` work.
