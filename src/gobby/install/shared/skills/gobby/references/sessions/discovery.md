# Discover sessions

Load when resolving identity, browsing work, registering a hookless client, or
changing a title. Fetch schemas for `gobby-sessions:get_current_session`,
`get_session`, `list_sessions`, `session_stats`, `get_usage_breakdown`,
`register_session`, and `set_title` as needed.

Use the injected session reference first. Otherwise resolve the actual external
CLI identity and source with `get_current_session`. Never identify yourself with
`list_sessions(status="active", limit=1)`: simultaneous sessions make that unsafe.
Local `#N` references resolve in the caller project; use `<project>#N` or a UUID
for cross-project targets. The proxy wrapper carries caller context; a target
tool's `session_id` names the session being inspected.

Filter lists deliberately by project, source, status, and machine. MCP listing
returns a bounded count and total, with no cursor argument; do not invent one or
claim the bounded list is exhaustive. HTTP listing has its own cursor contract.
Read the selected record before interpreting its runtime or task relationships.
Usage breakdowns are aggregates, not proof that a task passed validation.

Hookless registration requires the client's real external identity and source.
Machine and project are resolved when omitted; a missing project requires
initialization or an explicit project. Registration is idempotent for identity,
and an ambient same-source identity mismatch is rejected. Resolve that mismatch
rather than registering a substitute identity. Do not register normal hooked
sessions again just to obtain a reference.

`set_title` sets a sticky manual title on the caller. Manual titles outrank
automatic claim titles and are inherited by bound clear successors. Operator
CLI maintenance includes `sessions renumber --project PROJECT` (preview until
`--apply`) and `backfill-context-windows --dry-run` (writes without that flag).
Renumbering changes display references; preserve UUIDs in durable integrations.

Operator `sessions delete` requires confirmation (`--yes` skips the prompt) and
uses the guarded session deletion lifecycle. HTTP clients additionally create
web-chat rows, resolve terminal context, rename, expire, update status, or bulk
move sessions. These are lifecycle operations, not ways to repair task claims or
handoff receipts. Inspect the target and returned errors before proceeding.

The operator can authorize a session to edit specific dirty paths that have no
live owner with `gobby sessions approve-dirty-paths SESSION_REF --checkout ROOT
PATHS...`. Agent sessions cannot run this command. Approval applies to that
checkout and the paths' current content; a new approval replaces the session's
previous approval for that checkout. Source:
`src/gobby/cli/sessions_dirty_paths.py::approve_dirty_paths`.

Guide: [Finding your session](../../../../../../../../docs/guides/sessions.md#finding-your-own-session).

_Last verified: 2026-10-17_
