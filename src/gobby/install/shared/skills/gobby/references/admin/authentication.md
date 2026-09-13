# Authentication

Load for browser login, daemon client authentication, token mismatch, password
reset, or credential rotation. These are operator procedures. Start with
`uv run gobby auth token` for file/hash agreement; it does not print the token
unless `--show` is supplied. Do not place plaintext credentials in transcripts.

The install-scoped token lives at `$GOBBY_HOME/local_cli_token`, normally
`~/.gobby/local_cli_token`, with owner-only permissions. Its authoritative hash
is managed by AuthStore. Operator clients use this token; managed agent runs
can use their scoped `GOBBY_AGENT_API_TOKEN`. Browser users authenticate with
their account and receive the `gobby_session` cookie.

1. Determine which credential path is failing. A missing local token, rejected
   browser cookie, and missing provider OAuth grant have different remedies.
2. Use current daemon clients to attach credentials automatically. For manual
   HTTP diagnostics, use the configured endpoint and bearer authentication;
   `/api/health` and `/api/admin/startup-progress` are public exceptions.
3. To repair or rotate the install token, the operator runs
   `gobby auth token --rotate` on the hub, then securely recopies the new token
   to trusted remote clients with mode 0600. Have clients reread the token and verify
   that the previous token is rejected. Browser sessions remain independent.
4. `gobby auth credentials` resets the sole installed user's password through
   a hidden confirmation prompt. Password reset revokes that user's browser
   sessions; sign in again. It does not create an alternate user or disable auth.
5. Verify the affected HTTP, MCP, hook, browser, or native client with a scoped
   read. Preserve a clear authentication error rather than retrying a mutation
   whose prior result is unknown.

Remote installation consumes the hub's existing token and does not generate or
rotate one. Store integration credentials through `secrets.md`; provider/MCP
OAuth uses the respective capability. Do not edit token hashes through generic
configuration, print `--show` output into evidence, or weaken authentication to
resolve a client configuration error.

For managed execution database access, the operator can inspect
`gobby postgres scoped-roles --json`; it lists active scoped roles without
credential material. `gobby postgres force-revoke-run EXECUTION_UUID` revokes
all scoped roles for that execution, with confirmation unless `--yes` is used.
An incomplete revocation reports a pending retry rather than success. Identify
the affected execution and coordinate its interruption before revoking access.

See [authentication](../../../../../../../../docs/guides/admin-operations.md#authentication)
and [HTTP authentication](../../../../../../../../docs/guides/http-endpoints.md).
