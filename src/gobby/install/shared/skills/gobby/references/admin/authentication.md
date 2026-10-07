# Authentication

Load for browser login, daemon client authentication, API-key mismatch, password
reset, or credential rotation. These are operator procedures. Do not place
plaintext credentials in transcripts.

Operator clients read `api_key` from `$GOBBY_HOME/bootstrap.yaml` with owner-only
permissions. gdaemon validates the key against the hub's `api_keys` table and
forwards its user, machine, and key IDs with a private per-boot secret. Python
accepts that complete identity only with the matching secret. Managed runs use
their scoped `GOBBY_AGENT_API_TOKEN`; browser users receive the `gobby_session`
cookie after account login.

1. Determine which credential path is failing. A missing API key, rejected
   browser cookie, and missing provider OAuth grant have different remedies.
2. Use current daemon clients to attach credentials automatically. For manual
   HTTP diagnostics, use the configured endpoint and bearer authentication;
   `/api/health` and `/api/admin/startup-progress` are public exceptions.
3. To repair or rotate a key, create a machine-bound key through API-key
   management, save the returned key and ID as `api_key` and `api_key_id` in that
   machine's bootstrap, then revoke the previous key. Clients reread bootstrap
   before new authenticated operations. Browser sessions remain independent.
   Rotating the daemon's bootstrap key also invalidates managed capabilities
   signed with its derived signing key; remint those capabilities.
4. `gobby auth credentials` resets the sole installed user's password through
   a hidden confirmation prompt. Password reset revokes that user's browser
   sessions; sign in again. It does not create an alternate user or disable auth.
5. Verify the affected HTTP, MCP, hook, browser, or native client with a scoped
   read. Preserve a clear authentication error rather than retrying a mutation
   whose prior result is unknown.

Remote installation requires a live API key for the node in its bootstrap and
the hub's `.secret_kek`; it does not generate or rotate the key. Store integration
credentials through `secrets.md`; provider/MCP OAuth uses the respective capability.
For hub outages, the server-side loopback break-glass procedure is documented in
the admin guide. Do not edit key hashes through generic configuration or weaken
authentication to resolve a client configuration error.

For managed execution database access, the operator can inspect
`gobby postgres scoped-roles --json`; it lists active scoped roles without
credential material. `gobby postgres force-revoke-run EXECUTION_UUID` revokes
all scoped roles for that execution, with confirmation unless `--yes` is used.
An incomplete revocation reports a pending retry rather than success. Identify
the affected execution and coordinate its interruption before revoking access.

See [authentication](../../../../../../../../docs/guides/admin-operations.md#authentication)
and [HTTP authentication](../../../../../../../../docs/guides/http-endpoints.md).
