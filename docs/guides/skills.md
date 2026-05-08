# Skills Guide

This guide covers how to create, install, discover, and load skills in Gobby
0.4.0. Skills follow the [Agent Skills specification](https://agentskills.io)
with Gobby-specific metadata for routing and context-aware injection.

## What a Skill Is

A skill is a reusable instruction bundle stored as a `SKILL.md` file. Gobby
stores installed skills in its database, keeps bundled skills synced from the
install tree, and exposes skills through both CLI commands and the
`gobby-skills` MCP server.

Gobby uses these scopes:

| Scope | How it is created | What it means |
|-------|-------------------|---------------|
| Bundled | Synced from `src/gobby/install/shared/skills/` | Ships with Gobby and is restored on bundled-content sync |
| Installed | `gobby skills install <source>` or `install_skill(project_scoped=false)` | Available globally in the local Gobby database |
| Project | `gobby skills install --project <source>` or `install_skill(project_scoped=true)` | Available for one project and preferred over a global skill with the same name |

`gobby skills init` creates `.gobby/skills/` and a local config file for
project skill authoring. Installing a skill persists it through the daemon and
marks it as either installed or project-scoped.

## SKILL.md Format

Every skill needs YAML frontmatter followed by Markdown instructions:

```markdown
---
name: commit-message
description: Generate conventional commit messages following project conventions
version: "1.0.0"
license: MIT
compatibility: Requires git CLI
allowed-tools: Bash(git:*)
metadata:
  skillport:
    category: git
    tags: [git, commits, conventions]
    alwaysApply: false
  gobby:
    triggers: [commit, git commit]
    audience: all
    task_categories: [code]
    format_overrides:
      autonomous: full
---

# Commit Message Generator

Use conventional commit format:
- `feat:` for new behavior
- `fix:` for bug fixes
- `docs:` for documentation-only changes
```

### Required Fields

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | Lowercase letters, digits, and hyphens; starts with a letter |
| `description` | string | Discovery text shown in lists and search results |

### Optional Fields

| Field | Type | Notes |
|-------|------|-------|
| `version` | string | Top-level or `metadata.version`; normalized to a string |
| `license` | string | License identifier |
| `compatibility` | string | Requirements or limitations |
| `allowed-tools` | string/list | Tool patterns associated with the skill |
| `alwaysApply` | boolean | Legacy top-level auto-injection flag |
| `injectionFormat` | string | Skill default format: `summary`, `full`, or `content` |
| `category` | string | Top-level category; also supported under `metadata.skillport.category` |
| `internal` | boolean | Hide from generic discovery by default |
| `triggers` | string/list | Top-level trigger phrases, split on commas when a string |
| `metadata` | object | Namespaced extension data |

The parser also accepts `allowed_tools` as an alias for `allowed-tools`.

## Metadata

### `metadata.skillport`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `category` | string | none | Used for CLI and MCP category filtering |
| `tags` | list | `[]` | Used by search and generated docs |
| `alwaysApply` | boolean | `false` | Legacy auto-injection flag when no audience config is present |

Top-level `alwaysApply` and `category` take precedence over nested
`metadata.skillport` values.

### `metadata.gobby`

Gobby reads this namespace for context-aware skill selection:

| Field | Type | Notes |
|-------|------|-------|
| `audience` | string | `all`, `interactive`, `autonomous`, or a workflow-specific agent type |
| `depth` | int/list/string | Exact depth, list of depths, or range such as `"0-2"` |
| `steps` | list | Workflow step names where the skill applies |
| `task_categories` | list | Task categories such as `code`, `docs`, or `research` |
| `sources` | list/string | Session sources where the skill applies |
| `format_overrides` | object | Per-audience format override |
| `priority` | int | Lower values inject earlier |
| `internal` | boolean | Alternate location for the internal-skill flag |

When `metadata.gobby` contains audience keys, Gobby uses context-aware matching.
When it does not, Gobby falls back to the legacy `alwaysApply` behavior.

## Internal Skills

Use `internal: true` for shared methodology skills that are loaded by other
skills rather than chosen directly by a user.

- `list_skills` and `search_skills` hide internal skills by default.
- Pass `include_internal: true` to include them.
- `get_skill(name=...)` can load an internal skill directly.
- `internal` controls generic discovery; `metadata.gobby.audience` controls
  which session contexts should receive a skill.

## Directory Structure

### Minimal Skill

```text
my-skill/
└── SKILL.md
```

### Multi-File Skill

```text
my-skill/
├── SKILL.md
├── scripts/
│   └── setup.sh
├── references/
│   └── api.md
└── assets/
    └── template.json
```

Gobby stores text files from `scripts/`, `references/`, `reference/`, and
`assets/` as skill files. License files are classified separately, and common
binary formats are skipped.

## CLI Commands

These commands are implemented by `src/gobby/cli/skills.py`.

```bash
# List installed skills
gobby skills list
gobby skills list --category documentation --tags docs,api --enabled --limit 20
gobby skills list --json

# Show skill details
gobby skills show tech-writer
gobby skills show tech-writer --json

# Create local authoring files
gobby skills init
gobby skills new my-skill --description "Explain project release steps"

# Validate a skill file or directory
gobby skills validate ./my-skill
gobby skills validate ./my-skill/SKILL.md --json

# Install through the daemon
gobby skills install ./my-skill
gobby skills install --project ./my-skill
gobby skills install github:owner/repo/path/to/skill
gobby skills install clawdhub:commit-message

# Search hubs and manage hub config
gobby skills search "code review"
gobby skills search "code review" --hub skillsmp --limit 10 --json
gobby skills hub list
gobby skills hub add company-skills --type github --repo myorg/skills

# Manage installed skills
gobby skills update my-skill
gobby skills update --all
gobby skills remove my-skill
gobby skills enable my-skill
gobby skills disable my-skill

# Metadata and generated docs
gobby skills meta get my-skill skillport.category
gobby skills meta set my-skill skillport.category docs
gobby skills meta unset my-skill skillport.tags
gobby skills doc --format markdown
gobby skills doc --format json --output skills.json
```

Commands that install, update, remove, search hubs, or list hubs call the
daemon-backed `gobby-skills` MCP server. Local listing, showing, metadata
editing, enabling, disabling, scaffolding, validation, and doc generation read
or write the local database and filesystem directly.

## MCP Tools

Use progressive discovery before calling a skill tool:

1. `list_mcp_servers`
2. `list_tools(server_name="gobby-skills")`
3. `get_tool_schema(server_name="gobby-skills", tool_name="...")`
4. `call_tool(server_name="gobby-skills", tool_name="...", arguments={...})`

Current `gobby-skills` tools:

| Tool | Purpose |
|------|---------|
| `list_skills` | Lightweight installed/project skill metadata |
| `get_skill` | Full skill content and file metadata |
| `get_skill_file` | One stored file from a multi-file skill |
| `search_skills` | Search installed/project skills |
| `install_skill` | Install from local path, GitHub reference, ZIP, or hub reference |
| `update_skill` | Refresh an installed skill from its source |
| `remove_skill` | Soft-delete a skill |
| `restore_skill` | Restore a soft-deleted skill |
| `move_skill_to_project` | Move an installed skill into project scope |
| `move_skill_to_installed` | Move a project skill back to installed scope |
| `list_hubs` | List configured skill hubs |
| `search_hub` | Search all hubs or one named hub |

Common calls:

```python
call_tool(
    server_name="gobby-skills",
    tool_name="list_skills",
    arguments={"limit": 50, "include_internal": False},
)

call_tool(
    server_name="gobby-skills",
    tool_name="search_skills",
    arguments={"query": "documentation", "top_k": 5, "include_internal": True},
)

call_tool(
    server_name="gobby-skills",
    tool_name="get_skill",
    arguments={"name": "tech-writer"},
)

call_tool(
    server_name="gobby-skills",
    tool_name="install_skill",
    arguments={"source": "skillsmp:code-review", "project_scoped": True},
)

call_tool(
    server_name="gobby-skills",
    tool_name="search_hub",
    arguments={"query": "testing", "hub_name": "skillsmp", "limit": 10},
)
```

`list_skills` accepts `category`, `enabled`, `limit`, `session_id`, and
`include_internal`. `search_skills` accepts `query`, `category`, `tags_any`,
`tags_all`, `top_k`, `session_id`, and `include_internal`.

## Skill Hubs

Gobby 0.4.0 configures these hub types by default:

| Hub | Type |
|-----|------|
| `anthropic-skills` | `github-collection` |
| `claude-plugins` | `claude-plugins` |
| `clawdhub` | `clawdhub` |
| `skillsmp` | `skillsmp` |

Hub references use `hub:slug` syntax, for example
`gobby skills install skillsmp:code-review`. GitHub references can use
`owner/repo`, `owner/repo#branch`, `github:owner/repo`, or
`https://github.com/owner/repo/tree/branch/path/to/skill`.

## Skill Injection

Skill injection is selected by `src/gobby/skills/injector.py`.

Selection order:

1. Agent skill profiles can include or exclude explicit skill names.
2. Skills with `metadata.gobby` audience config are matched against agent type,
   depth, workflow step, task category, and session source.
3. Skills without audience config use legacy `alwaysApply`.

Format resolution order:

1. Agent profile `default_format`
2. `metadata.gobby.format_overrides[agent_type]`
3. Skill `injectionFormat`

The global skills config supports:

```yaml
skills:
  inject_core_skills: true
  injection_format: summary
```

Rules that load or advertise skills should use semantic lifecycle events such
as `turn_start` and `turn_end`. Raw provider hook names are runtime details.

## Behavioral Skill Validation

Bundled skills that materially change agent behavior should have a pressure
scenario under `tests/skills/scenarios/<skill-name>/`. Each scenario records a
run with the skill excluded and a run with the skill loaded, then asserts the
behavioral delta.

Run focused skill scenarios with:

```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/ -m skill_tdd
```

## Best Practices

1. Keep each skill focused on one repeatable task.
2. Write the description for discovery; it is the text agents see first.
3. Use `internal: true` for helper methodology that should stay out of generic
   lists.
4. Use `metadata.gobby` audience fields for context-specific injection.
5. Put long references in `references/` and load them only when needed.
6. Validate skill files before installing or publishing them.
7. Install project-specific behavior with `--project` so it does not affect
   unrelated projects.

## See Also

- [Agent Skills Specification](https://agentskills.io)
- [Rules Guide](./rules.md)
- [MCP Tools Guide](./mcp-tools.md)
- [Variables Guide](./variables.md)

_Last verified: 2026-05-07_
