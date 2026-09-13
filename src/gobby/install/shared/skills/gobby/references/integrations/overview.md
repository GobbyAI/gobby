# Integrations

Load when connecting provider hooks, external issue trackers, webhook receivers,
or extension packages. `$gobby integrations references` lists topics; menus
do not enable automation, send events, install plugins, or update external issues.

1. Load `hooks.md` for provider hook wiring, native transport and verification hooks.
2. Load `webhooks.md` for outbound hook/pipeline delivery and inbound GitHub intake.
3. Load `plugins.md` before choosing an extension mechanism.
4. Load `external.md` for GitHub/Linear setup, import, synchronization and recovery.

Discover the actual external MCP server before its tools. A connected server is
not an installed skill. Use `mcp-servers` for connection/authentication setup,
`skills` for guidance installation, `rules` for semantic enforcement, and
`pipelines` for workflow execution. Fetch current schemas before agent tool calls.

Agent task mutations use `gobby-tasks` or `gobby-tasks-ops`; integration CLI and
HTTP administration are operator procedures. Establish project/repository scope
and authorization before external writes or automatic processing. Inspect saved
configuration and readiness rather than inferring activation from templates.
Test mutations only against temporary state and controlled receivers. An error
or unavailable connector is not evidence that no external work exists.

See [integrations](../../../../../../../../docs/guides/integrations.md) and
[extensions](../../../../../../../../docs/guides/webhooks-and-plugins.md).
