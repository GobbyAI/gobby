# Template catalog and authoring

Load when choosing or authoring a parameterized MCP definition. Discover installed
catalog entries through `list_mcp_servers`. Operator inspection commands are
`gobby mcp-proxy list-templates` and `show-template NAME`, with `--global` for the
global catalog. A listing is discovery, not proof that a template is enabled.

Bundled templates live under `src/gobby/install/shared/mcp/templates/` and sync
to `mcp_server_templates`. Project templates use `.gobby/mcp/templates/`; machine
templates use `~/.gobby/mcp/templates/`. A same-named override of a Gobby-owned
template must declare `override: true`. Project rows shadow global rows even
when disabled; instantiation returns `template_disabled` rather than falling
back to the global definition. Preserve Gobby-owned templates; customize in the
supported user/project roots.

Use the schema in `src/gobby/install/shared/mcp/AGENTS.md` and
`MCPServerTemplate.from_definition`. Define transport/runtime fields, named
parameters, secret references, choices, defaults, and conditional requirements.
`expand_template` is the shared expansion path. Unknown parameter names and
missing required/one-of/conditional values are errors; use the reported contract
to correct the request, not ad hoc runtime-field overrides.

Parameter values are strings. A secret default may be a forward reference;
missing required secrets produce a persisted instance needing configuration,
while absent optional secrets are omitted from runtime arguments/environment.
Read [authentication](authentication.md) before configuring credentials.

Operator `gobby sync` imports supported template/instance files. Sync preserves
installed enabled choices and user ownership. Removal of an instance YAML never
deletes its installed row. Author and validate examples in temporary state before
publishing changes from the serving checkout.

The bundled OpenAPI template describes one API per instance, pinned to
`awslabs.openapi-mcp-server@1.1.5`. Inspect its current parameter contract before
setting authentication, spec paths/URLs, tag filters, or output repair. Treat
its tools as instance tools; it does not make prompts/resources proxy-visible.

Guide: [Templates and instances](../../../../../../../../docs/guides/mcp-tools.md#templates-and-instances).

_Last verified: 2026-09-12_
