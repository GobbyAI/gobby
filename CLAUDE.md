# CLAUDE.md

@AGENTS.md

## Claude Code Specifics

- Task management MCP calls (gobby-tasks) are allowed during plan mode — planning
  includes organizing work, not just designing it.
- Nested CLAUDE.md files load on demand when working under their directories:
  `crates/`, `src/gobby/dispatch/`, `src/gobby/install/shared/` (and its
  `workflows/rules/` subtree).
