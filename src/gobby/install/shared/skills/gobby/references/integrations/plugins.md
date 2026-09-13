# Plugins and extension selection

Load when asked to install or author a plugin, or when an old example names a
Python workflow action. First identify which application's plugin system the
request means. A provider plugin package, installed skill, MCP server and Gobby
workflow extension are different resources.

1. For reusable guidance, discover/install a skill through the skills capability.
2. For callable operations, configure an MCP server through mcp-servers; inspect
   the registered tool schema and the server's actual connection state.
3. For deterministic automation, author a rule or pipeline using those capabilities.
4. For external notifications, load `webhooks.md` and choose the supported surface.
5. For provider-owned packages, use that provider's installation mechanism and
   inspect the resulting hooks/skills/tools. Installation is an operator action;
   a package's presence does not prove its tools or rules are active.

Gobby has no supported custom Python workflow-action plugin dispatcher or
runtime hook-plugin API. `WebhookAction` is a parse/serialize model, not an
executable `action: webhook` step. Do not revive retired plugin/reload commands
or promise callbacks and response capture from that model.

When extension setup fails, identify the owning surface and repair its actual
configuration. Keep user/project overrides and unrelated provider entries.
Use the admin capability for coordinated install repair and the skills capability
for scripts and skill lifecycle. See the
[extension guide](../../../../../../../../docs/guides/webhooks-and-plugins.md#plugin-development-status).
