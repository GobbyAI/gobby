# Skills Guide

This guide covers how to create, install, discover, and load skills in Gobby.
Skills follow the [Agent Skills specification](https://agentskills.io)
with Gobby-specific metadata for routing and context-aware injection.

## What a Skill Is

A skill is a reusable instruction bundle stored as a `SKILL.md` file. Gobby
stores installed skills in its database, keeps bundled skills synced from the
install tree, and exposes skills through both CLI commands and the
`gobby-skills` MCP server.

Installed rules and agent requirements can request skill loads. Inspect their
current rows before claiming that a rule is active. A load supplies instructions;
it does not perform the operations they describe or grant permission to do so.
Memory retrieval, for example, remains an explicit `gobby-memory` operation.

Gobby uses these scopes:

| Scope | How it is created | What it means |
|-------|-------------------|---------------|
| Bundled content | Synced from `src/gobby/install/shared/skills/` into Gobby-owned installed rows | Source templates; not a separate live scope |
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
| `audience` | string | Exactly `all`, `interactive`, `autonomous`, `orchestrator`, or `worker` |
| `depth` | int/list/string | Exact depth, list of depths, or range such as `"0-2"` |
| `steps` | list | Workflow step names where the skill applies |
| `task_categories` | list | Task categories such as `code`, `docs`, or `research` |
| `sources` | list/string | Session sources where the skill applies |
| `format_overrides` | object | Per-audience format override |
| `priority` | int | Lower values inject earlier |
| `internal` | boolean | Alternate location for the internal-skill flag |

The parser recognizes these audience fields and rejects other audience names.
They describe intended selection context; they are not proof of automatic body
injection. See [Instruction selection](#instruction-selection).

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

## Complete Loading And Levels

Use `get_skill(name="<installed-name>")`; `brief=true` is the default and keeps
instructions exact while omitting management metadata. Request `brief=false`
when you need IDs, provenance, metadata, versioning or hashes. Read `skill.content`
and follow `page.next_cursor` with another call containing only `cursor` until
it is null. Each page must be its own outer tool result. Preserve content order,
whitespace and multibyte text; load multiple skills sequentially after deduping.

Only a complete entrypoint records the skill in caller session state. Use the
outer `call_tool` session context; a listing, manifest, menu, partial load, HTTP
read or CLI show does not satisfy an instruction requirement. Tracking is best
effort without usable session context. On `stale_cursor` or `invalid_cursor`,
restart the original lookup and read all pages. Restart if content is absent or
explicitly truncated; UI collapse alone is not truncation. Resolve scan errors
before applying external content.

Leveled skills declare `metadata.gobby.levels` and optionally `default_level`.
For example, `get_skill(name="brevity", level="max")` prepends the active level
to the complete body. Omission chooses the declared default or first level.
Unknown levels and levels on an unleveled skill fail with `invalid_level`.
Cursor continuations retain the original level/view. Completion records the
level separately from the loaded-skill set; reference loads do not overwrite it.

## Reference Files

After reading the entrypoint, load only topics whose stated conditions apply.
Fetch the `get_skill_file` schema before the first call in a context, then use
its exact name/path, for example:

```python
call_tool(
    server_name="gobby-skills",
    tool_name="get_skill_file",
    arguments={"name": "gobby", "path": "references/tasks/closing.md"},
)
```

Read `file.content` and follow the content cursor exactly as for an entrypoint.
A completed reference records a separate exact identity such as
`gobby:references/tasks/closing.md`. It loads neither its parent skill nor a
sibling topic. Router or menu loads do not satisfy an exact reference gate.
Context resets clear reference tracking wherever skill-load tracking resets.

The initial manifest is bounded. `get_skill_files` lists attached-file metadata
with `path_prefix`, `file_type`, and `after_path` filters; retain the filters and
continue using `next_after_path` until no next path remains. This is keyset
listing, not content pagination. Listing a file never loads it. Use exact stored
paths rather than absolute paths, traversal segments or guessed filenames.

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
gobby skills install github:owner/repo
gobby skills install https://github.com/owner/repo/tree/main/path/to/skill
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

Call `list_skills`, `get_skill`, and `search_skills` directly through
`call_tool`; these bootstrap tools are exempt from the schema gate. For other
known `gobby-skills` tools, call `get_tool_schema` directly when no
current-context lease exists, then use `call_tool`. Use `list_tools` only when
the tool name is unknown.

Current `gobby-skills` tools:

| Tool | Purpose |
|------|---------|
| `list_skills` | Lightweight installed/project skill metadata |
| `get_skill` | One exact entrypoint page; brief or management view |
| `get_skill_file` | One exact page from a stored file |
| `get_skill_files` | Bounded, keyset-paginated attached-file metadata |
| `materialize_skill_scripts` | Prepare script files and report runtime readiness |
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

The source configuration supplies these defaults; use `list_hubs` for active providers:

| Hub | Type |
|-----|------|
| `anthropic-skills` | `github-collection` |
| `gobby-topic` | `github-topic` |
| `claude-plugins` | `claude-plugins` |
| `clawdhub` | `clawdhub` |
| `skillsmp` | `skillsmp` |

Hub references use `hub:slug` syntax, for example
`gobby skills install skillsmp:code-review`. GitHub references can use
`owner/repo`, `github:owner/repo#branch`, `github:owner/repo`, or
`https://github.com/owner/repo/tree/branch/path/to/skill`.

Hub search can return `hub_errors` with successful partial results. A failed hub
is not evidence that it has no matching skill. Preserve the result's exact
install ID; GitHub topic IDs can contain both repository and skill path.
Inspect `auth_configured` and secret-reference names without exposing values.
`gobby skills hub add --type github` stores a `github-collection` provider;
other accepted CLI types and required flags are listed by `hub add --help`.
The add command uses revision-checked configuration writes. There is no
`gobby skills hub remove`; update configuration or choose a different name.

## Installation And Update Boundaries

MCP installation supports local paths, local ZIP files, GitHub references and
configured hub references. It rejects arbitrary HTTP URLs. Existing local paths
win over ambiguous owner/repo strings; use explicit GitHub references or absolute
local paths to avoid ambiguity with the daemon's working directory. A source
containing multiple skills installs only the first through MCP. Select the exact
skill path when that is not intended.

Installation scans the entrypoint and attached text files and publishes them
atomically. External sources fail closed when scanning is unavailable. Inspect
scan failures, source provenance and name/scope conflicts before choosing a
replacement; installation is not loading. Bundled template paths cannot be
installed or moved into project scope: keep custom sources outside the bundle.

`update_skill` refreshes local/filesystem and GitHub sources, validates and
rescans changes, and restores the stored revision/files on a failed update.
Inspect `updated`, `skipped` and `skip_reason`: unsupported hub/ZIP provenance or
missing source metadata can be a successful skip, not a refresh. CLI
`update --all` likewise requires inspecting per-skill outcomes.

HTTP import differs from MCP: local/ZIP imports require a project ID and remain
inside its registered machine-local checkout after symlink resolution. HTTP
collection imports return per-item errors as well as imported items; do not
treat transport success as complete installation.

## Script Materialization

After loading a skill that requires scripts, fetch the schema and call
`materialize_skill_scripts(name="<installed-name>")`. It returns an absolute
`scripts_dir`, `files_written`, and environment values for executing the skill's
documented command. The path identifies a content-addressed cache generation;
do not assume scripts live beside the agent's checkout or manually edit the
cache as an authoring fix.

Inspect `parser_deps.installed` and its warning, `browser.ready` and its warning,
and `node.version`/`satisfies_floor`. A returned directory does not guarantee Node,
parser dependencies or browser readiness. Materialization publishes scripts
atomically and can prepare managed runtime artifacts for trusted sources, but
does not execute the task script. Use only a fallback the skill documents.

External-source scripts are scanned and cannot install executable dependencies
or managed browser artifacts. Trusted Node preparation requires a lockfile and
uses `npm ci --ignore-scripts`. `metadata.gobby.runtime` supports `cli` fields
(`npm`, `version`, `bin`), a `node` floor of `>=MAJOR.MINOR.PATCH`, and semantic
`skill_release`; declarations confer no execution authority. Preserve the
returned environment, including the browser cache location, and resolve again
after correcting source or runtime failures. Do not remove another process's
live staging directory.

## Lifecycle And HTTP Management

MCP `remove_skill` soft-deletes; `restore_skill` restores by ID or name.
`move_skill_to_project` requires the skill ID and target project ID;
`move_skill_to_installed` returns it to global scope. Conflicting names require
resolution before moving. Soft-delete retention defaults to 30 days, so a
permanently purged skill cannot be assumed restorable.

Operator HTTP DELETE permanently purges a row that is already soft-deleted;
repeating DELETE is not a harmless retry. HTTP export emits SKILL.md content,
not a complete backup of attached files or every management field. Enumerate
and retain the full inventory for portability. HTTP file PUT updates an existing
attached file; list it first. The HTTP guide maps all management endpoints.

CLI enable/disable and metadata commands change stored state, not loaded
instructions. Current local name-based CLI show/enable/disable/metadata calls
resolve global rows; use project-aware or ID-based management for overrides.
Disabled skills remain retrievable by direct `get_skill`; enabled state controls
discovery/selection and is not an authorization boundary.

Bundled synchronization updates Gobby-owned entrypoints and files while keeping
custom skills. Ordinary drift preserves an enabled toggle; restoring a deleted
bundle re-enables it. Removed bundled entrypoints are soft-deleted. Old project
rows sourced from the bundled template tree are stale shadows and are purged;
real custom project sources must live outside that tree. Inspect sync errors
and installed rows before claiming a successful cutover. HTTP restore-defaults
runs this synchronization, not a blanket reset of user-owned skills.

## Instruction Selection

Skills are discovered on demand. Hook discovery reads enabled DB-backed skills
with a filesystem fallback; explicit loads record completed instructions. Agent
profiles, task requirements and installed rules may constrain discovery or
request loads. Do not infer an active gate from source metadata.

The parser retains audience, `alwaysApply`, `injectionFormat` and
`format_overrides` metadata. The current code does not implement the old guide's
universal audience/depth/step selection and three-tier body-format precedence;
these fields alone do not establish that a body was injected. Global skills
configuration includes `inject_core_skills` and `injection_format` values
`summary`, `full`, or `none`, describing manifest preferences rather than
completed instruction delivery. Use a tracked explicit load when required.

Rules that request skills should use semantic lifecycle events such as
`turn_start` and `turn_end`, with shared instruction-fetch helpers so exact
reference requirements and standalone levels remain intact.

## Behavioral Skill Validation

Bundled skills that materially change agent behavior should have a pressure
scenario under `tests/skills/scenarios/<skill-name>/`. Each scenario records a
run with the skill excluded and a run with the skill loaded, then asserts the
behavioral delta.

Run focused skill scenarios with:

```bash
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_writing_skills_skill.py -m skill_tdd
```

Bundled SKILL.md and reference files must each fit both character and UTF-8 byte
counts under `skills.bundled_max_content_size` (default 15000). Authoring tests
fail violations; sync warns without truncating. Split semantically, state exact
conditional reference calls, and normally keep workflows within three loaded
references. Scripts/assets/notices and non-bundled files are outside this ceiling.

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

_Last verified: 2026-09-12_
