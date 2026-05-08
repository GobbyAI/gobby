# Prompts

Gobby prompts are DB-managed templates used by workflows, agents, and runtime
surfaces. Bundled prompt files seed the database, while global and project
overrides customize behavior.

## Mental Model

The database is the runtime source of truth. Bundled prompt files under
`src/gobby/install/shared/prompts/` are synchronized into the database at startup
or through `gobby sync`. Runtime prompt loading follows this precedence:

1. Project override.
2. Global override.
3. Bundled prompt.

The loader renders templates with Jinja2 `StrictUndefined` and falls back to a
simple format path where supported. Bundled prompts are read-only outside
development mode; customize behavior through project or global overrides.

## Quick Start

List synced content changes for prompts:

```bash
uv run gobby sync --type prompts --verbose
```

Dry-run prompt export:

```bash
uv run gobby export prompt
```

Export project prompt overrides to the global Gobby directory:

```bash
uv run gobby export prompt --global
```

Import prompts from another project:

```bash
uv run gobby import prompt --from-project /path/to/project
```

Use the Configuration page for browser editing:

```text
http://localhost:60889/#configuration
```

## Precedence And Scope

Prompt records have one of three scopes:

| Scope | Use |
|-------|-----|
| `bundled` | Shipped defaults synced from install files |
| `global` | User-wide override |
| `project` | Current project override |

Project overrides win because they are closest to the active repository. Global
overrides are useful for personal defaults across projects. Bundled prompts are
the fallback and should remain portable.

## Sync

Bundled prompt sync parses prompt markdown and frontmatter, then writes prompt
records to the database. It tracks prompt path, variables, version, and source
path. The generic sync command can sync only prompts:

```bash
uv run gobby sync --type prompts
```

Use `--verify-only` to check bundled content integrity without syncing:

```bash
uv run gobby sync --type prompts --verify-only
```

In production mode, sync verifies bundled content integrity unless `--force` is
provided. In development mode, sync is allowed without that production integrity
gate.

## Export And Import

The export/import CLI works with file-backed resource directories in `.gobby/`:

```bash
uv run gobby export prompt NAME --to /path/to/other/project
uv run gobby export prompt NAME --global
uv run gobby import prompt NAME --from /path/to/prompt.md
uv run gobby import prompt --from-project /path/to/other/project
```

The Configuration API also supports config export and import that includes prompt
overrides. Use this when moving UI-managed configuration between environments.

## CLI

Prompt-related operator commands:

```bash
uv run gobby sync --type prompts
uv run gobby export prompt [NAME]
uv run gobby import prompt [NAME] --from PATH
uv run gobby import prompt --from-project PROJECT_PATH
```

Use `gobby sync` for bundled content. Use `gobby export` and `gobby import` for
sharing overrides.

## HTTP

Configuration routes expose prompt overrides:

```text
GET    /api/config/prompts
GET    /api/config/prompts/{path}
PUT    /api/config/prompts/{path}
DELETE /api/config/prompts/{path}
GET    /api/config/export
POST   /api/config/import
```

The Web UI Configuration page uses these routes for prompt editing and
configuration import/export.

## MCP

There is no dedicated public `gobby-prompts` MCP server. Agents interact with
prompts indirectly through workflows, skills, rules, and provider calls. If an
agent needs to inspect prompt-related tools, use progressive discovery against
the relevant owning server, such as `gobby-config`, `gobby-workflows`, or
`gobby-skills`.

## File Locations

- `src/gobby/prompts/loader.py`: runtime loading, precedence, rendering, cache.
- `src/gobby/prompts/sync.py`: bundled prompt sync.
- `src/gobby/storage/prompts.py`: prompt records and persistence.
- `src/gobby/install/shared/prompts/`: bundled prompt markdown.
- `src/gobby/servers/routes/configuration.py`: prompt override HTTP routes.
- `src/gobby/cli/sync.py`: bundled content sync CLI.
- `src/gobby/cli/export_import.py`: resource export/import CLI.
- `.gobby/prompts/`: project prompt override files.

## See Also

- [configuration.md](configuration.md)
- [workflows-overview.md](workflows-overview.md)
- [skills.md](skills.md)
- [mcp-tools.md](mcp-tools.md)

_Last verified: 2026-05-08_
