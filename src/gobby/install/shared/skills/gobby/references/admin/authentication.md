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
3. To enroll a configured remote node, run `gobby auth login`. It prompts for
   the hub account credentials and pins the hub certificate. Inspect the enrolled
   key's hint and ID with `gobby auth key --show`; this never prints the key.
   Rotate it with `gobby auth key --rotate`. Rotation mints and publishes a new
   machine-bound key, verifies authentication, then revokes the old key. Failed
   verification restores only the prior key fields, preserving other bootstrap
   settings. Concurrent key changes are preserved; follow any reported key IDs
   and cleanup instructions. Clients reread bootstrap before new authenticated
   operations. Browser sessions remain independent. Rotation invalidates
   outstanding managed capability tokens; remint those capabilities.
   Use `gobby auth key --mint LABEL` for an additional key without changing
   bootstrap. It prints the key once, with its hint and ID; store it securely,
   because the key is not shown again. The `--show`, `--rotate`, and `--mint`
   options are mutually exclusive and cannot accompany subcommands.
   `gobby auth key list` lists your keys with ID, hint, label, machine, creation,
   last use, and revocation timestamps, marking the bootstrap's current key.
   `gobby auth key revoke KEY_ID` revokes one of your other keys. It refuses the
   bootstrap's own key; use `--rotate` for that key. A foreign or missing ID is
   reported as not found among your keys. These commands use the bootstrap key
   as bearer authentication and the pinned hub certificate on remote nodes.
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
