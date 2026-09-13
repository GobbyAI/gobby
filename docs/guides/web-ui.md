# Web UI

The Web UI provides Chat, its Activity panel, and a Settings overlay for a
Gobby daemon. The daemon owns sessions and operational state; the browser is
a client of its authenticated HTTP and WebSocket interfaces.

## Quick Start

Inspect or start the daemon from its serving checkout:

```bash
uv run gobby status
uv run gobby start --verbose
```

The installed application normally uses `http://localhost:60887`. A source
development server normally uses port 60889. Use the actual addresses reported
by `gobby status`; bootstrap can change the ports and host.

The installer establishes the canonical account. To reset the sole installed
user's password, run:

```bash
uv run gobby auth credentials
```

The reset revokes that user's browser sessions. Sign in again with the account
email and new password; restarting the daemon is not required for the reset.
The HTTP-only `gobby_session` cookie authorizes browser API calls and the
`/ws` proxy. Local daemon tokens remain a separate client credential path.

## Navigation

`web/src/App.tsx` renders Chat as the page surface. Select the project in the
header, use Chat's Activity panel for its available work views, and open the
Settings control for configuration. Activity selection is component state,
not a set of top-level hash routes.

Earlier guides listed standalone Project, Dashboard, Reports, Traces, and
other navigation pages. Those entries and `appNavigation.tsx` no longer
describe the application shell. Do not use `#dashboard`, `#projects`, or
`#traces` as supported routes. The trace backend still exists, but Activity
deliberately hides its Traces tab; see [observability](observability.md).

Project selection comes from the shell. A hub-visible project does not imply
that the current machine has its checkout; honor checkout-required messages
before attempting filesystem or source-control operations.

## Web Chat

Chat combines HTTP session reads with WebSocket delivery. Its main owners are
`web/src/components/chat/ChatPage.tsx` and `web/src/hooks/useChat/`.

- Session creation and replay use session HTTP routes; web-chat creation uses
  `/api/sessions/web-chat`.
- Provider and model controls read the provider catalog. Source freshness is
  reported separately from last-good model facts.
- Session viewing, attachment, and continuation have distinct interaction
  modes. Observe their visible mode and delivery status before sending input.
- Voice and artifact events share the browser's live connection. Use the
  respective guides for provider setup.

Attachment limits come from the effective `chat.attachment_max_file_bytes`,
`chat.attachment_max_total_bytes_per_message`, and
`chat.attachment_max_files_per_message` settings. The effective total cannot
exceed the per-file limit multiplied by the file count. Use smaller files or
fewer attachments when validation rejects the payload; changing a limit is an
operator configuration update, not an automatic retry remedy.

For CLI-session delivery, `INVALID_ATTACHMENT` means the supplied attachments
failed validation and must be corrected. `ATTACHMENT_ERROR` reports a processing
failure; preserve the error, check service health, and inspect delivery state
before retrying. The implementation owners are
`src/gobby/servers/chat_attachment_limits.py` and
`src/gobby/servers/websocket/handlers/session_observe_proxy.py`.

An uncertain send result is not permission to replay a mutation. Preserve the
displayed error and session identity, reconnect, and inspect the conversation
before retrying.

See [sessions](sessions.md), [providers and models](providers-and-models.md),
and [voice](voice.md) for their operating contracts.

## CLI

Persistent `ui.enabled` controls whether the daemon manages the UI. Production
mode serves the built application through the daemon HTTP server. Development
mode, and automatic mode when source is available, use the frontend dev server.
Discover explicit development, build, status, and exposure operations with:

```bash
uv run gobby ui --help
```

Use the Configuration capability to inspect desired and active settings before
changing them. Follow any reported restart requirement and coordinate active
sessions before restarting. A login failure alone is not a reason to rebuild
the UI or restart the daemon.

## HTTP

1. Read `gobby status` and the public health/startup responses. Distinguish a
   process that answers HTTP from completed startup readiness.
2. Check authentication. `/api/auth/status` is public; stateful API calls
   require valid credentials.
3. Inspect the browser's failed HTTP request or WebSocket close reason and
   record the affected session and timestamp. Match that evidence to bounded
   daemon logs before choosing a repair.
4. After the repair, repeat the failed read and verify the live connection.

Manual authenticated checks, from a trusted client with the local token:

```bash
BASE="${GOBBY_DAEMON_URL:-http://localhost:60887}"
TOKEN="$(tr -d '\r\n' < "${GOBBY_HOME:-$HOME/.gobby}/local_cli_token")"
curl -fsS "$BASE/api/health"
curl -fsS "$BASE/api/auth/status"
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE/api/admin/status"
```

Keep the token out of logs and shared evidence. See
[admin authentication](admin-operations.md#authentication) for rotation and
[HTTP endpoints](http-endpoints.md) for route ownership and access rules.

## MCP

The browser and MCP proxy operate the same domain state. Agents should use
the relevant Gobby MCP tools for task lifecycle, configuration, sessions,
memory, and other supported operations. Discover a known tool's schema before
its first use; discover tool or server names only when unknown.

Browser inspection is useful for UI-specific behavior, but it does not replace
domain lifecycle tools. A connected browser MCP server is not an installed
skill, and UI visibility does not grant permission to perform an operation.

## File Locations

- `web/src/App.tsx`: authenticated shell, project selection, Chat, and overlays.
- `web/src/components/chat/`: Chat and Activity panel.
- `web/src/components/settings/`: Settings overlay and sections.
- `web/src/hooks/`: client API, session, settings, and connection state.
- `src/gobby/servers/routes/`: HTTP route owners.
- `src/gobby/servers/websocket/`: live protocol and delivery owners.

## See Also

- [Admin operations](admin-operations.md)
- [Observability](observability.md)
- [gclient](gclient-user-guide.md)
- [Frontend development](frontend-style-guide.md)

_Last verified: 2026-09-13_
