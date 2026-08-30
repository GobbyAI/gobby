# MCP Server Templates

This directory holds bundled MCP **server templates**, not live server processes.
They sync to `mcp_server_templates` the same way rules and skills sync to their
registry tables. The database is the source of truth for what is active.

See `../AGENTS.md` for the shared template-vs-enforcement contract, including
`enabled` defaults and drift-refresh of Gobby-owned rows.

## Layout

- `templates/<name>.yaml` — parameterised server definitions (this tree)
- Project override roots: `.gobby/mcp/templates/` and `.gobby/mcp/servers/`
- Machine override roots: `~/.gobby/mcp/templates/` and `~/.gobby/mcp/servers/`

A user/project template that reuses a bundled name must set `override: true`.
Loading fails when a same-named copy shadows a bundled template without that
label. A disabled template still occupies its `(name, project_id)` slot and
shadows a same-named global template; instantiation surfaces refuse it with
`template_disabled` instead of expanding it.

## Template schema

```yaml
name: openapi
description: Short description
version: 1
enabled: true
transport: stdio
command: uvx
args: ["awslabs.openapi-mcp-server@1.1.5"]
connect_timeout: 120
env: {}
headers: {}
runtime_hook: chrome_executable_path   # optional; copied onto instances
override: false
params:
  - name: token
    env: API_TOKEN          # materialises into config.env
    arg_flag: --api-key     # appends [flag, value] to config.args
    required: true
    secret: true
    default: none
    default_secret: github_personal_access_token
    choices: ["none", bearer]
    description: Human-facing help
require_one_of:
  - [spec_url, spec_path]
require_when:
  - {param: auth_type, equals: bearer, requires: [auth_token]}
```

`default_secret` applies when the param is omitted: the instance stores
`$secret:<default_secret>` and reports the name as missing if the secret is
unset.

## Secret references

Secret params accept a *reference*, never a credential:

- `$secret:<name>` is always accepted as a forward reference (the secret may be
  set later).
- A bare name is accepted only when it matches the secret-name grammar and the
  named secret already exists in the instance's project-then-global scope.
- Any other string is rejected. The error names the parameter and tells the
  caller to write `$secret:<name>`; it does not echo the supplied value.

Normalised secret values in `template_values` are always `$secret:<name>`.

## Instance YAML

Instances live beside templates, not in this bundled tree:

```yaml
# .gobby/mcp/servers/lightspeed.yaml  or  ~/.gobby/mcp/servers/lightspeed.yaml
name: lightspeed            # optional; defaults to the file stem
template: openapi           # independent of the instance name
description: Lightspeed X-Series retail API
enabled: true
values:
  api_name: lightspeed
  api_base_url: https://example.retail.lightspeed.app/api
  spec_url: https://x-series-api.lightspeedhq.com/openapi/x-series.json
  auth_type: bearer
  auth_token: $secret:lightspeed_api_token
```

Committed instance files use `$secret:<name>` so a fresh clone can sync before
the secret exists. Project files instantiate into that project's `mcp_servers`
rows; files under `~/.gobby/mcp/servers/` instantiate as global. Removal of an
instance file does not delete the row; removal is explicit.
