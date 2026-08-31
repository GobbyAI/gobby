---
name: mcp-servers
description: Discover MCP templates, instantiate them with scope and secrets, and call instance tools through progressive discovery.
version: "1.0.0"
category: core
triggers: mcp server, template, openapi, instantiate, add_mcp_server, secrets
metadata:
  gobby:
    audience: all
---

# MCP Servers

Templates are parameterised server definitions. Instances are live
`mcp_servers` rows expanded from a template. The daemon runs one process per
instance. Name resolution is the caller's project first, then global.

Nothing is instantiated by `gobby install`. Instantiation is explicit.

## Templates vs instances

Bundled templates sync from `src/gobby/install/shared/mcp/templates/`. Project
overrides live at `.gobby/mcp/templates/<name>.yaml`; machine overrides at
`~/.gobby/mcp/templates/<name>.yaml`. A same-named copy of a bundled template
must set `override: true`.

Instances are hub rows. Declare them as YAML (committed, no secret values) or
create them through MCP/CLI/HTTP.

## Listing

```python
result = call_tool("gobby", "list_mcp_servers")
# result["templates"] — catalog visible in this scope
# result servers list — instantiated rows, not the catalog
```

CLI: `gobby mcp-proxy list-templates` and `gobby mcp-proxy list-servers`.

## Instantiating

```python
call_tool("gobby", "add_mcp_server", {
    "name": "github",
    "template": "github",
    "values": {"token": "$secret:github_personal_access_token"},
    "scope": "project",  # or "global"
})
```

CLI: `gobby mcp-proxy add-server <name> --template <template> [--global]`.

Secret params accept a `$secret:<name>` reference only. A raw credential is
rejected. If the result is `needs_configuration`, set the secret in the same
scope, then refresh:

```text
gobby secrets set NAME          # project scope inside a registered repo
gobby secrets set NAME --global
gobby mcp-proxy refresh --server NAME
```

`gobby secrets set` prints the scope it wrote.

## Declarative instance YAML

```yaml
# .gobby/mcp/servers/github.yaml          project
# ~/.gobby/mcp/servers/github.yaml        machine / global
name: github
template: github
enabled: true
values:
  token: $secret:github_personal_access_token
```

Commit the file; never commit secret values. `gobby sync` upserts the hub row
and does not delete rows when a file is removed — removal is
`remove_mcp_server`. After sync, missing secrets still show
`needs_configuration` until `gobby secrets set` and refresh.

## Calling instance tools

Instance names are the `name` you chose (defaults to the template name when
you keep them the same). Progressive discovery:

```python
list_tools("github")
get_tool_schema("github", "<tool>")
call_tool("github", "<tool>", {...})
```

A session in another project does not see this project's instances.

## Rotation

Change the secret with `gobby secrets set NAME` in the instance's scope, then
`gobby mcp-proxy refresh --server NAME`. The next call uses the new value; no
daemon restart.

## OpenAPI template

Pin is `awslabs.openapi-mcp-server@1.1.5`. First connect is typically 5–20 s
(cold `uvx` plus spec fetch); `connect_timeout` is 120 s.

Params: `api_name`, `api_base_url`, `spec_url` | `spec_path`, `auth_type`
(`none`|`bearer`|`api_key`|`basic`), `auth_token`, `auth_api_key`,
`auth_api_key_name`, `auth_api_key_in`, `auth_username`, `auth_password`,
`include_tags`, `exclude_tags`, `allow_insecure_http`,
`allow_private_networks`, `output_validation` (`strict`|`repair`|`off`),
`repair_null_policy` (`drop`|`empty`).

`output_validation: strict` (default) makes the upstream server reject any
response that drifts from the spec. `repair` turns that check off and the proxy
repairs each result against the spec's response schema instead: a null in a
non-nullable field is dropped or, with `repair_null_policy: empty`, replaced
by `""`/`0`/`false`/`[]`/`{}`; parseable scalars are coerced to the declared
type; every change is listed under `schema_deviations` in the result. `off`
disables both.

Constraints:

- Spec must be OpenAPI 3.0 or 3.1 with no external `$ref`.
- HTTPS unless `allow_insecure_http` is `"true"`.
- Filter by tags only (`include_tags` / `exclude_tags`).
- One instance per API (no `ADDITIONAL_SPECS`, no Cognito).
- Tool names are slugified `operationId`s truncated at 56 characters.
- Prompts and resources are unproxied; tools only.
