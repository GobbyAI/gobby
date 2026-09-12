# Credentials and secret references

Load when configuring or rotating instance credentials. Discover the template's
secret parameters first. Prefer explicit `$secret:NAME` references in values,
environment, and headers wherever supported. Raw credentials do not belong in
versioned YAML or conversational tool arguments.

Template secret parameters accept `$secret:NAME` forward references. A bare name
is accepted only when it matches the secret-name grammar and already exists in
the instance's project-then-global lookup. Any other value is rejected without
echoing it. This is reference normalization, not raw-token acceptance.

Operator commands:

```bash
gobby secrets set NAME
gobby secrets set NAME --global
gobby mcp-proxy refresh --server INSTANCE
```

The first writes project scope inside a registered checkout; the second writes
global scope. Match the intended instance scope and inspect the printed scope.
Refresh resolves the visible project instance first, then global. A same-named
project instance can shadow the global one; an operator can select an exact
instance via the authenticated refresh API's `server_id`/scope fields.

Missing required secrets can leave an installed row with `needs_configuration`.
Set them and refresh; do not repeatedly add the server or claim connection based
on `success` alone. CLI add may prompt interactively for missing secrets; in
noninteractive use it prints configuration guidance and can exit zero while the
instance still needs configuration.

Rotation changes the stored secret then refreshes the instance. A daemon restart
is not required for that refresh path. OAuth has separate identity and browser
requirements: load [oauth](oauth.md).

Guide: [Authentication](../../../../../../../../docs/guides/mcp-tools.md#authentication).

_Last verified: 2026-09-12_
