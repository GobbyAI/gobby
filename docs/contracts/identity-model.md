# Identity Model

Gobby's current identity hierarchy is:

1. User owns machines.
2. Machine creates or forwards sessions.
3. Session owns runtime state, task links, transcripts, memory links, and agent lineage.

The user seam lives only on `machines.owner_user_id`. Session, task, agent, memory,
and workflow tables should derive user ownership by joining `session.machine_id` to
`machines.machine_id`, then reading `machines.owner_user_id`. Do not add parallel
`user_id` columns to those tables unless the identity contract changes.

## Machines

`machines.machine_id` is the primary key. The registry stores optional descriptive
metadata (`hostname`, `os`, `label`, `tailscale_name`) plus `first_seen`,
`last_seen`, and nullable `owner_user_id`.

`owner_user_id` intentionally has no foreign key yet. It is a forward-compatible
seam for future auth and fleet ownership without forcing a user table or auth
provider into the local-first stack today.

Missing and placeholder machine ids are not registry rows. Blank values and legacy
fallbacks such as `unknown` and `unknown-machine` mean the client did not provide a
real machine identity.

## Daemon Access Credentials

Daemon auth establishes access to one operator-controlled daemon. The
install-scoped `local_cli_token` authorizes CLI, hook, MCP, HTTP, and direct
WebSocket clients. Web credentials in `auth.username` and the scrypt
`auth.password_hash` create `gobby_session` browser sessions.

These credentials represent daemon access capabilities. They do not populate
`machines.owner_user_id`, identify the calling machine, or add row-level user
ownership to tasks, sessions, memory, agents, or workflows. Machine identity
continues to come from the client-supplied `machine_id` contract.

See [Secrets Contract](./secrets.md#daemon-api-token) for token storage, header,
and rotation semantics.

## Sessions

`sessions.machine_id` remains the client-supplied machine identity used in the
session natural key. Session APIs may filter by `machine_id` for multi-machine
views and shared Postgres hubs.

The daemon should not infer a remote client's user by writing user ownership onto
the session. The derivation is:

`session -> sessions.machine_id -> machines.machine_id -> machines.owner_user_id`

## Future Fleets

Fleet daemons and nodes should register as machines first, then associate ownership
through `owner_user_id` when multi-user authorization exists. A node can host many
sessions; a user can own many machines; a shared stack can serve many users without
changing the session schema.
