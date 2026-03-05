# Skills Guide

This guide covers how to create, manage, and use skills in Gobby. Skills follow the [Agent Skills specification](https://agentskills.io) and are compatible with SkillPort.

## Overview

Skills are reusable instructions that teach AI agents how to perform specific tasks. They're stored as `SKILL.md` files and can be:

- **Core skills**: Bundled with Gobby, always available
- **Project skills**: Stored in `.gobby/skills/` for project-specific workflows
- **User skills**: Stored in `~/.gobby/skills/` for personal preferences
- **Installed skills**: Downloaded from GitHub or other sources

## SKILL.md File Format

Skills use Markdown with YAML frontmatter:

```markdown
---
name: commit-message
description: Generate conventional commit messages following project conventions
license: MIT
compatibility: Requires git CLI
metadata:
  author: your-name
  version: "1.0.0"
  skillport:
    category: git
    tags: [git, commits, conventions]
    alwaysApply: false
  gobby:
    triggers: ["/commit"]
allowed-tools: Bash(git:*)
---

# Commit Message Generator

Instructions for the AI agent go here...

## Format

Use conventional commits format:
- `feat:` new feature
- `fix:` bug fix
- `docs:` documentation only
- `refactor:` code refactoring
```

## Frontmatter Fields

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique skill identifier (kebab-case) |
| `description` | string | Brief description for discovery and matching |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `license` | string | License identifier (MIT, Apache-2.0, etc.) |
| `compatibility` | string | Notes on requirements or limitations |
| `allowed-tools` | string/list | Tool patterns the skill may use |
| `metadata` | object | Nested metadata namespaces |

### Metadata Namespaces

The `metadata` field contains namespaced data for different systems:

#### `metadata.skillport` (SkillPort compatibility)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `category` | string | - | Skill category for filtering |
| `tags` | list | `[]` | Tags for search and discovery |
| `alwaysApply` | boolean | `false` | Auto-inject at session start |

#### `metadata.gobby` (Gobby-specific)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `triggers` | list | `[]` | Slash commands that invoke this skill |
| `project_scope` | string | - | Limit to specific project patterns |
| `workflow_binding` | string | - | Bind to workflow step |

#### `metadata.author` / `metadata.version`

Top-level metadata for authorship:

```yaml
metadata:
  author: anthropic
  version: "1.2.0"
```

## Directory Structure

### Minimal Skill (single file)

```
skills/
└── my-skill/
    └── SKILL.md
```

### Full Skill Structure

```
skills/
└── my-skill/
    ├── SKILL.md          # Required: main skill file
    ├── scripts/          # Optional: executable scripts
    │   └── setup.sh
    ├── references/       # Optional: reference materials
    │   ├── api-docs.md
    │   └── examples.md
    └── assets/           # Optional: images, templates
        └── template.json
```

### Core Skills Location

Gobby's built-in skills are in:

```
src/gobby/install/shared/skills/
├── gobby-tasks/
│   └── SKILL.md
├── gobby-sessions/
│   └── SKILL.md
├── gobby-memory/
│   └── SKILL.md
├── gobby-mcp/
│   └── SKILL.md
└── ...
```

### User and Project Skills

| Location | Scope | Priority |
|----------|-------|----------|
| `~/.gobby/skills/` | User-global | Low |
| `.gobby/skills/` | Project-specific | High |
| `.claude/skills/` | Claude Code compatible | Medium |

Project skills override user skills with the same name.

## Examples

### Simple Documentation Skill

```markdown
---
name: api-docs
description: Document REST API endpoints following OpenAPI conventions
metadata:
  skillport:
    category: documentation
    tags: [api, openapi, rest]
---

# API Documentation Guidelines

When documenting API endpoints:

1. Use OpenAPI 3.0 format
2. Include request/response examples
3. Document error codes
4. Add authentication requirements
```

### Core Skill (Always Applied)

```markdown
---
name: code-review
description: Review code for bugs, security issues, and best practices
metadata:
  skillport:
    category: review
    tags: [code-review, quality]
    alwaysApply: true
---

# Code Review Standards

Always check for:
- Security vulnerabilities (OWASP Top 10)
- Error handling completeness
- Test coverage
- Documentation
```

### Skill with Triggers

```markdown
---
name: test-runner
description: Run tests with coverage and reporting
metadata:
  gobby:
    triggers: ["/test", "/coverage"]
  skillport:
    category: testing
    tags: [testing, pytest, coverage]
allowed-tools: Bash(pytest:*, coverage:*)
---

# Test Runner

## Usage

Invoke with `/test` or `/coverage`.

## Commands

- Full test suite: `pytest tests/ -v`
- With coverage: `pytest --cov=src --cov-report=html`
```

### Skill with References

```markdown
---
name: database-migrations
description: Manage SQLite database schema migrations
metadata:
  skillport:
    category: database
    tags: [sqlite, migrations]
references:
  - docs/migration-guide.md
  - examples/migrations/
---

# Database Migrations

See the referenced migration guide for detailed instructions.
```

## CLI Commands

```bash
# List all skills
gobby skills list

# List with JSON output (for CI)
gobby skills list --json

# Show skill details
gobby skills show commit-message

# Create new skill
gobby skills new my-skill

# Initialize skill in current directory
gobby skills init

# Install from GitHub
gobby skills install github:user/repo/path/to/skill

# Install from local path
gobby skills install /path/to/skill-dir

# Update installed skill
gobby skills update my-skill

# Remove skill
gobby skills remove my-skill

# Validate skill format
gobby skills validate /path/to/SKILL.md

# Generate documentation
gobby skills doc my-skill

# Get/set metadata
gobby skills meta get my-skill version
gobby skills meta set my-skill version "2.0.0"
gobby skills meta unset my-skill custom-field

# Enable/disable skill
gobby skills enable my-skill
gobby skills disable my-skill
```

## MCP Tools

The `gobby-skills` server provides programmatic access:

```python
# List all skills
mcp__gobby__call_tool(
    server_name="gobby-skills",
    tool_name="list_skills",
    arguments={"category": "git"}
)

# Get skill details
mcp__gobby__call_tool(
    server_name="gobby-skills",
    tool_name="get_skill",
    arguments={"name": "commit-message"}
)

# Search skills by query
mcp__gobby__call_tool(
    server_name="gobby-skills",
    tool_name="search_skills",
    arguments={"query": "testing coverage", "limit": 5}
)

# Install skill
mcp__gobby__call_tool(
    server_name="gobby-skills",
    tool_name="install_skill",
    arguments={"source": "github:user/repo/skills/my-skill"}
)
```

## Skill Injection

### Core Skills (alwaysApply)

Skills with `metadata.skillport.alwaysApply: true` are automatically injected at session start. The injection format is configurable:

```yaml
# In ~/.gobby/config.yaml
skills:
  inject_core_skills: true
  injection_format: summary  # summary, full, or none
```

### Task-Based Recommendations

When using `suggest_next_task`, Gobby recommends relevant skills based on task category:

| Task Category | Recommended Skills |
|---------------|-------------------|
| `code` | gobby-tasks, gobby-expand, gobby-worktrees |
| `test` | gobby-tasks, gobby-expand |
| `docs` | gobby-tasks, gobby-plan |
| `config` | gobby-tasks, gobby-mcp |
| `refactor` | gobby-tasks, gobby-expand, gobby-worktrees |
| `planning` | gobby-tasks, gobby-plan, gobby-expand |
| `research` | gobby-tasks, gobby-memory |

## Best Practices

1. **Keep skills focused**: One skill, one purpose
2. **Write clear descriptions**: Help discovery and matching
3. **Use appropriate categories**: Aid filtering and organization
4. **Add tags**: Improve search results
5. **Include examples**: Show expected behavior
6. **Document requirements**: Note dependencies in `compatibility`
7. **Version your skills**: Track changes with `metadata.version`

## See Also

- [Agent Skills Specification](https://agentskills.io)
- [SkillPort](https://github.com/gotalab/skillport) - Compatible skill format
- [Rules Guide](./rules.md) - Integrate skills with rules
- [MCP Tools Guide](./mcp-tools.md) - Full MCP tool reference
