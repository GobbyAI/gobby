# Rule diagnostics

Load for missing rules, unexpected blocks, absent injections, or management
errors. Start with `list_rules(brief=true)`, then `get_rule` for the exact installed
name. Inspect definition, provenance, enabled state, priority, and drift.

The MCP list has event/group/enabled filters and no public pagination; event
wins over group when both are supplied. Brief output is name/event/group/enabled.
Malformed bodies can be skipped with a warning, so an empty filtered list does
not prove a row is absent. Use exact lookup and daemon diagnostics. Retrieve
oversized lists through `gobby-results`, following complete `next_offset` pages.

Check: actual normalized event; active enforcement configuration; installed row
and project scope; audience/agent scope; current selectors; conditions and effect
selectors; earlier block/hard-coded override; provider delivery and receipt.
A skill directive without completed fetch is not a delivery failure. Read errors
rather than repeating them.

Operator commands: `gobby rules list`, `show NAME`, `enable NAME`, `disable NAME`,
`import FILE`, `export --group GROUP`, and `audit --session SESSION --limit 5
--json`. Enable/disable use the daemon; list/import/export/audit use CLI database
access. Audit is bounded history, not proof every allow/skip was recorded. Replay
events in fixtures, never a live session.

HTTP operators can list/get/create/update/delete/toggle and list groups/tags.
There is no `PUT /api/rules` collection replacement. Per-name updates can replace
a body; bulk-toggle has its own route. Missing names, invalid bodies, bundled
protection, duplicates, and partial bulk results need distinct recovery. Preserve
row/export evidence before authorized changes and read back afterward.

If changed code has no effect, identify the daemon checkout before coordinating
restart. For row-only changes inspect selectors and scope first. Do not directly
mutate the live database or toggle all policy as a diagnostic experiment.

Guides: [Tooling](../../../../../../../../docs/guides/rules.md#public-tooling),
[CLI](../../../../../../../../docs/guides/cli-commands.md#rules-and-pipelines),
[HTTP](../../../../../../../../docs/guides/http-endpoints.md#memory-skills-workflows-and-rules).

_Last verified: 2026-09-12_
