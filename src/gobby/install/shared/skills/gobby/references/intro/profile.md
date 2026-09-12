# Working profile

Load before creating or changing the global user profile. First read
[onboarding](onboarding.md) when the owner location or prerequisites are unknown.
This captures durable preferences once and updates them deliberately; do not
copy a profile into a repository or recreate the legacy personal path.

## Discover and preserve

Resolve this process's bootstrap ownership. On the local owner, read
`<files_home>/USER.md`; on a remote node, GET
`{hub_daemon_url}/api/files/user-md` using the hub's existing local CLI credential.
Keep tokens out of prose, examples and command output. An absent profile file
returns empty content; a missing files-home root is a configuration failure.

Read existing content before asking questions or writing. Infer only low-risk,
relevant facts from context: global Git name/email, shell, OS, and obvious
editor/terminal preferences. Do not collect unrelated personal data. Ask targeted
questions only for missing facts that materially affect agent behavior, and do
not infer broad authority from a one-time task instruction.

## Write

Use exactly these sections, in order:

1. **Identity:** name, role, common project context and stable machine/tool facts.
2. **Working Style:** sequencing, planning, directness and quality expectations.
3. **Preferences:** tools, validation, documentation, formatting and communication.
4. **Autonomy & Boundaries:** permitted actions, required approvals and assumptions.
5. **Never-Do:** explicit hard prohibitions and privacy/security boundaries.

Move useful existing content into the closest section. Remove duplicates and
verified stale contradictions; preserve unresolved preferences rather than
inventing replacements. Keep the file concise and free of task-specific status.

On the owner, publish to the existing `<files_home>/USER.md` atomically. The
owner's HTTP API provides that publication path too: PUT `/api/files/user-md`
with JSON `{"content": "<complete revised profile>"}`. A remote node sends that
PUT directly to `hub_daemon_url`, never to a node-local profile directory.
The write replaces the whole profile; read and reconcile concurrent changes
before replacement. There is no revision-check parameter on this route.

Read back the saved profile and report the path/URL, sections created or changed,
and facts the user chose not to provide. Empty content clears the profile; use it
only for an intended clearing request. The decoded UTF-8 limit is 1,048,576 bytes;
remain far below it for concise session context.

## Failures and recovery

- Missing root: stop profile publication and resolve operator provisioning;
  never silently mkdir the configured files-home root or fall back to
  `~/.gobby/personal/USER.md`.
- Authentication/network failure: verify owner origin and existing credential
  configuration, without generating/rotating a token as a profile-writing step.
- `remote_target`: the request hit a remote daemon; select the configured owner.
- Invalid JSON/content or oversized body: repair the request, then retry only
  after preserving the current profile. Do not assume a failed write cleared it.

See the [HTTP contract](../../../../../../../../docs/guides/http-endpoints.md#global-working-profile).
Changing the file does not retroactively replace already-loaded session context.
