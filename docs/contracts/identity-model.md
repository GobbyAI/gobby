# Identity Model

Gobby's current identity hierarchy is:

1. User owns machines.
2. Machine creates or forwards sessions.
3. Session owns runtime state, task links, transcripts, memory links, and agent lineage.

The user seam lives only on `machines.owner_user_id`. Session, task, agent, memory,
and workflow tables should derive user ownership by joining `session.machine_id` to
`machines.id`, then reading `machines.owner_user_id`. Do not add parallel
`user_id` columns to those tables unless the identity contract changes.

## Machines

`machines.id` is an install-generated UUID primary key. The registry stores optional descriptive
metadata (`hostname`, `os`, `label`, `tailscale_name`) plus `first_seen`,
`last_seen`, and nullable `owner_user_id`.

`owner_user_id` intentionally has no foreign key yet. It is a forward-compatible
seam for future auth and fleet ownership without forcing a user table or auth
provider into the local-first stack today.

Missing machine attribution is represented by an absent value. Sentinel strings are
retired and never become registry rows.

## Daemon Access Credentials

Daemon auth establishes access to one operator-controlled daemon. The
install-scoped `local_cli_token` authorizes CLI, hook, MCP, HTTP, and direct
WebSocket clients. Web credentials in `auth.username` and the scrypt
`auth.password_hash` create `gobby_session` browser sessions.

These credentials represent daemon access capabilities. They do not populate
`machines.owner_user_id`, identify the calling machine, or add row-level user
ownership to tasks, sessions, memory, agents, or workflows. The daemon stamps its
install-generated machine UUID on session registration and web-chat creation.

See [Secrets Contract](./secrets.md#daemon-api-token) for token storage, header,
and rotation semantics.

## Sessions

`sessions.machine_id` is a nullable UUID foreign key to `machines.id` and remains
part of the session natural key. `NULLS NOT DISTINCT` keeps unattributed session
registration idempotent. Session APIs may filter by `machine_id` for multi-machine
views and shared Postgres hubs.

The daemon should not infer a remote client's user by writing user ownership onto
the session. The derivation is:

`session -> sessions.machine_id -> machines.id -> machines.owner_user_id`

## Future Fleets

Fleet daemons and nodes should register as machines first, then associate ownership
through `owner_user_id` when multi-user authorization exists. A node can host many
sessions; a user can own many machines; a shared stack can serve many users without
changing the session schema.
