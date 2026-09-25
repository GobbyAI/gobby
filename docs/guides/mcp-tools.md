# Gobby MCP Tools

Use the proxy to discover only the tool schemas needed for the current task.
The current runtime has 22 internal registries, including `gobby-ask`, plus
explicitly installed external server instances. The live inventory and selected
tool schema own names and parameters. Behavioral contracts remain normative;
a discrepancy needs investigation, not a blanket assumption that either an old
example or a successful call proves correct behavior.

## Tool Surfaces

Native proxy tools are called directly. Internal `gobby-*` tools and external
instance tools are reached through `call_tool`. An external MCP connection is
not an installed skill. Bundled names such as `github` and `playwright` are
server templates until someone instantiates them.

The stdio carrier registers `list_mcp_servers`, `list_tools`, `get_tool_schema`,
`call_tool`, `recommend_tools`, `search_tools`, `add_mcp_server`,
`remove_mcp_server`, `import_mcp_server`, `init_project`, `set_variable`, and
`get_variable`. Its `init_project` returns a CLI-required error; initialize a
project with operator command `gobby init`.

The daemon's direct MCP endpoint additionally exposes `status` and
`read_mcp_resource` and does not register `init_project`. Its signatures are
not identical to the stdio carrier. In particular, stdio `list_mcp_servers`
accepts caller `session_id`; direct daemon MCP also accepts name/scope/project
filters. Do not pass direct-daemon-only parameters to a stdio tool.

## Progressive Discovery Pattern

- Known leased tool: call it through `call_tool`.
- Known unleased tool: fetch `get_tool_schema`, then call it.
- Unknown tool name on a known server: `list_tools`, then the chosen schema.
- Unknown server or explicit inventory: `list_mcp_servers`.

Each step is its own native tool call. Never nest discovery through `call_tool`
or eagerly fetch all schemas. A schema lookup creates a current-context lease;
validation failures return the current schema and preserve the lease for a
corrected retry. Inventory observations survive context reset. Schema leases
survive ordinary resume/restart but reset on clear, compact, or reconstructed
context.

```python
get_tool_schema(server_name="gobby-tasks", tool_name="get_task")
call_tool(server_name="gobby-tasks", tool_name="get_task",
          arguments={"task_id": "#42"}, session_id="#12910")
```

Replace example references with actual task and caller identities. Wrapper
`session_id` belongs outside `arguments`; a target tool's session argument is
for a different target session. Wrapper `project_id` selects an authorized
cross-project call. Local `#N` references resolve in the caller's project;
foreign session targets should use UUIDs or `<project>#N`. Prefer a structured
`arguments` object over the `args` alias.

Skill bootstrap tools `get_skill`, `list_skills`, and `search_skills` bypass the
schema gate. `get_skill_file` and `get_skill_files` do not. Read every returned
cursor page to complete a skill/reference load; a menu or preview is not a load.

`recommend_tools` supports llm (default), semantic, and hybrid selection.
`search_tools` searches tool metadata, optionally filtered by server. Candidate
results do not create schema leases or grant execution authority. Missing
search dependencies can be diagnosed while using explicit inventory discovery.

## Templates And Instances

`list_mcp_servers` returns visible instances with connection state and a
`templates` catalog. Pending lazy instances need not be broken. Templates are
not live processes and catalog visibility is not proof of enabled state.
Operator commands `gobby mcp-proxy list-templates [--global]` and
`gobby mcp-proxy show-template NAME [--global]` inspect parameter contracts.

The hub tables `mcp_server_templates` and `mcp_servers` own installed state.
Bundled templates sync from `src/gobby/install/shared/mcp/templates/`. User roots:

| Scope | Templates | Instance declarations |
| --- | --- | --- |
| Project | `.gobby/mcp/templates/` | `.gobby/mcp/servers/` |
| Machine/global | `~/.gobby/mcp/templates/` | `~/.gobby/mcp/servers/` |

A template overriding a Gobby-owned name must set `override: true`. A project
row shadows a same-named global template even when disabled; instantiation
returns `template_disabled`. `gobby install` lists templates without creating
instances. Sync imports declarations but removing an instance YAML does not
delete its installed row.

Template authoring uses `MCPServerTemplate.from_definition` and the schema in
`src/gobby/install/shared/mcp/AGENTS.md`. The shared `expand_template` path
validates named parameters, choices, required values, `require_one_of`, and
`require_when`. Unknown names fail. Parameter values are strings. Templates
own expanded runtime fields; change their parameters instead of patching those
fields directly on an instance.

## Manage Instances

Use native `add_mcp_server` with `name`, `template`, `values`, and explicit
`scope="project"` or `"global"`. For manual servers, provide transport plus a
URL for HTTP/SSE/WebSocket or a command for stdio. Native signatures do not
expose all HTTP configuration fields; OAuth setup is described below.

Operator examples:

```bash
gobby mcp-proxy add-server github --template github
gobby mcp-proxy add-server github --template github --global
gobby mcp-proxy remove-server github --global
```

Project lookup for use is project-first then global. Update/removal targets the
exact selected scope, without mutation fallback to another scope. Same-scope
duplicate adds fail. A successful add can persist the row without connecting:
inspect `connected`, `needs_configuration`, `missing_secrets`, and `configure`.
Fix the existing instance rather than repeatedly adding it.

Declarative instance example:

```yaml
# .gobby/mcp/servers/github.yaml
name: github
template: github
enabled: true
values:
  token: $secret:github_personal_access_token
```

The name may be omitted to use the file stem. Commit secret references only.
Retirement requires removing the source declaration and explicitly removing the
installed row. Recreating a row changes OAuth identity.

`import_mcp_server` accepts a source project, GitHub URL, or query; `servers`
only narrows project import, and is not itself an import source. Prefer one
source per call. Review imported/skipped/failed outcomes. Importing server
configuration may connect to external services or launch programs and needs
appropriate task authority.

Authenticated HTTP PUT/PATCH `/api/mcp/servers/{name}` updates an instance; it
cannot rename one. Templated runtime-field edits return `template_owned_fields`.
There is no corresponding native MCP update tool. See [HTTP endpoints](http-endpoints.md#mcp-proxy).
Agent task lifecycle continues to use task MCP tools rather than CLI/HTTP
shortcuts that bypass workflow tracking.

## Authentication

Template secrets accept explicit `$secret:NAME` forward references. A bare
secret name is normalized only if it matches the name grammar and already
exists in project-then-global lookup. Other values are rejected without echoing
them. Required missing secrets leave the instance needing configuration;
optional missing secrets are omitted from the expanded runtime environment/args.

Operator `gobby secrets set NAME` writes project scope inside a registered
checkout; `--global` selects global storage. Inspect its printed scope. CLI add
may prompt for missing secrets in a TTY; noninteractive add can exit zero with
`needs_configuration` and recovery commands. Exit zero alone does not prove a
connected server.

After setting or rotating a secret, use `gobby mcp-proxy refresh --server NAME`.
This refresh resolves project before global; an exact instance can be selected
through the HTTP refresh API with `server_id` and scope. Rotation plus refresh
does not require a daemon restart. For browser consent use [OAuth](mcp-oauth.md).

### OpenAPI Instances

The bundled `openapi` template pins `awslabs.openapi-mcp-server@1.1.5`, with
`connect_timeout: 120`. Provide `api_name`, `api_base_url`, and either `spec_url`
or `spec_path`; local spec paths must be absolute because daemon cwd differs
from project cwd. Use one instance per API. Authentication choices are none,
bearer, api_key, and basic, with conditional secret requirements. The template
also exposes tag filters and explicit insecure-HTTP/private-network controls.
Inspect its installed contract before selecting values.

Output validation defaults to strict upstream checking. `output_validation=repair`
disables that upstream check and lets the proxy repair results against the
response schema; `off` disables both. Repair records changes under
`schema_deviations`. `repair_null_policy=drop` removes nonnullable nulls; `empty`
uses typed empty values. Supported scalar coercions are also reported. Missing
spec/schema lookup leaves the response unchanged; repair mode is not proof that
every response was repaired. The stdio carrier forwards tools, not downstream
prompts or resources.

## Diagnostics

Use `list_mcp_servers` and operator `gobby mcp-proxy status --json` to distinguish
pending, disabled, failed, and connected state. Check identity, scope, transport,
and credentials before retrying a call. Source templates are not proof of live
configuration.

`gobby mcp-proxy refresh --server NAME` reloads the instance and refreshes tool
schemas/hashes and changed embeddings. A filtered refresh can pick up a newly
synced row. Unfiltered refresh can touch multiple visible servers. `--force`
treats all tools as new for indexing; it does not override disabled state.
Inspect `stats.by_server` errors even when top-level success is true.

Schema mismatches need fresh discovery and corrected arguments. Search failures
may need embedding/model configuration; explicit `list_tools` remains a separate
discovery lane. The HTTP embedding endpoint is an operator/client maintenance
surface. Coordinate shared daemon restarts with active sessions and use the
serving checkout.

## Oversized Results

The proxy can replace large successful text results with an envelope containing
`offloaded`, `result_id`, `retrieval_available`, character counts, a preview,
and guidance. Optional `call_tool(intent=...)` can add relevant matched sections.
A preview or search match is not the complete original output.

Discover `gobby-results` and fetch the chosen schema:

- `search_tool_result` searches chunks of one stored result; default limit 5,
  maximum 50. Search is relevance-based rather than cursor pagination.
- `get_tool_result` returns ordered character slices; default limit 1000. Follow
  its returned `next_offset` until null. Live envelope limits may shorten slices,
  so do not advance by the requested limit or by UTF-8 byte count.

Results are project-scoped and expire according to live retention configuration.
Not-found can mean missing, expired, invalid, or inaccessible. If storage fails,
`retrieval_available=false` means the omitted tail cannot be read from Gobby.
Over-cap results return `reason="too_large"` without storage. Narrow a source
read query or use source pagination; never blindly repeat a mutating operation.

Failed and non-text results are not ordinary text-offload candidates. Instruction
entrypoint/reference loads and result retrieval have mandatory exemptions.
Tool-result slices are distinct from instruction cursor pages and never satisfy
instruction requirements.

## Internal Registries

Discover actual tools and schemas on demand. This directory replaces duplicated
per-tool lists whose signatures and counts had drifted from their owning guides.
Detailed task, session, memory, workflow, and other procedures remain in their
capability guides and agent reference library.

| Registry | Responsibility |
| --- | --- |
| `gobby-tasks` | Task lifecycle, dependencies, labels, and task queries |
| `gobby-tasks-ops` | Task artifacts, stages, expansion, build, and delivery |
| `gobby-plans` | Plan registry |
| `gobby-profiles` | Build profiles |
| `gobby-feedback` | Feedback batches and outcomes |
| `gobby-results` | Stored oversized tool results |
| `gobby-sessions` | Sessions, transcripts, handoffs, and terminals |
| `gobby-memory` | Persistent knowledge |
| `gobby-review-learning` | Review lesson recall and capture |
| `gobby-workflows` | Rules, variables, definitions, and pipelines |
| `gobby-ask` | Durable source-bound questions and assigned worker evidence |
| `gobby-metrics` | Usage and observability |
| `gobby-agents` | Agent lifecycle and inter-session messaging |
| `gobby-worktrees` | Worktree isolation lifecycle |
| `gobby-workspaces` | Workspaces, tabs, and panes by ref, with pane input, reads, and waits; the same rows as [`gobby workspaces` and `gobby panes`](cli-commands.md#workspaces), the [`gclient` window](gclient-user-guide.md#workspaces), and the [workspace messages](../contracts/gterm-protocols.md#workspace-messages) |
| `gobby-clones` | Clone isolation lifecycle |
| `gobby-merge` | Merge operations |
| `gobby-hub` | Machine/project and cross-project queries |
| `gobby-config` | Configuration schema and revisions |
| `gobby-voice` | Voice vocabulary |
| `gobby-skills` | Installed instructions and hubs |
| `gobby-cron` | Scheduled operations |
| `gobby-communications` | External channels and identities |

Guarded tools can be hidden from ordinary discovery. A registry's existence is
not permission to use its internal stage operations.

## Error Handling

Envelope shape depends on the carrier. Stdio uses REST envelopes commonly
containing `success`, `result`, and timing metadata. Direct daemon MCP strips
a successful payload's redundant `success` field and exposes errors through MCP
error content. Inspect the actual carrier and nested tool outcome; never assume
all successful calls lack a result wrapper.

Parameter validation errors include the current target schema and retain its
lease. Repair the cause before retrying. Preserve `error_code`, `error_type`,
per-item failures, and offload/retrieval flags rather than interpreting a
transport success as a successful business operation.

## See Also

- [CLI commands](cli-commands.md#mcp-proxy)
- [HTTP endpoints](http-endpoints.md#mcp-proxy)
- [OAuth MCP servers](mcp-oauth.md)

_Last verified: 2026-09-12_
