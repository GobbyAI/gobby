# Sessions

Load when discovering session work, reading transcripts, preparing a context
boundary, or diagnosing terminal activity. Load the applicable topic completely
before acting; this overview and menus do not load sibling references. Fetch
unleased schemas and follow each reference cursor to completion.

Start with the injected session identity, or `gobby-sessions:get_current_session`
when it is missing. Use `get_session`, `list_sessions`, and `session_stats` for
discovery. Session records connect work and runtime identity; a status or an
archival summary does not prove a recoverable handoff exists.

Choose `$gobby sessions references discovery`, `transcripts`, `handoffs`,
`context`, `relationships`, `terminals`, `workspaces`, or `waits`. Help and menus only display
choices. Tool schemas own parameters and defaults. Agent operations use MCP;
the CLI and HTTP guide sections also describe operator/client maintenance.

If identity, ownership, or a pending boundary is unclear, inspect the record and
the appropriate topic before mutation. Preserve task ownership and recover
through the supported lifecycle; do not repair markers or claims directly.

Guide: [Session management](../../../../../../../../docs/guides/sessions.md).

_Last verified: 2026-09-12_
