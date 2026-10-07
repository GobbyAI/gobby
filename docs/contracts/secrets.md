# Secrets Contract

Gobby stores secret values in the hub. Decryption is limited to the daemon and
trusted local binaries that support standalone direct-hub mode. Remote clients
can create, replace, list metadata, and delete secrets; remote clients never
receive plaintext secret values, the DEK, or KEK material.

Daemon API keys follow a separate one-way verification contract. They grant
daemon access and never participate in secret-envelope encryption.

## Daemon API Key

| Surface | Contract |
| --- | --- |
| Plaintext | `api_key` and `api_key_id` in the owner-only startup bootstrap (default `~/.gobby/bootstrap.yaml`) |
| Hub verifier | SHA-256 hex digest in `api_keys.key_hash`, resolved by gdaemon with the key's user and machine ownership |
| Canonical HTTP credential | `Authorization: Bearer <gobby_ API key>` |
| Python operator identity | gdaemon strips client user, machine, key, and front-door-secret headers, then supplies verified ids with the per-boot `GOBBY_FRONT_DOOR_SECRET`; Python requires the matching secret |
| Browser credential | `gobby_session` cookie created by `/api/auth/login` |
| Break-glass credential | `break_glass` beside the daemon's bound bootstrap (default `~/.gobby/break_glass`), created once with mode `0600`; never logged or copied; the credential and its owner-only `.break_glass-staging` directory are read/write denied to managed sandboxes |

`X-Gobby-Break-Glass` admits an observed loopback HTTP peer holding the owner-only
break-glass credential before hub credential lookup. Grant routes still require
a valid grant. Remote peers and WebSocket connections cannot use it.

Local `gobby install` and hub startup provision a live API key. Remote machines
enroll with `gobby auth login`, which writes their own key into their bootstrap.
gdaemon resolves key bearers against the hub on HTTP and WebSocket upgrades.
Unknown, malformed, and revoked keys receive 401; an unavailable resolver receives
503 `key_resolver_unavailable`.

Clients read the bootstrap key for each new request or frame-stream hello.
After key replacement, new frame connections use the new key immediately;
existing frame streams remain attached. Revoking the old hub key stops new HTTP
and WebSocket authentication. Browser sessions remain independent.

Managed `gobby-agent-v1.` capabilities pass through gdaemon for Python to verify.
They sign with `HMAC-SHA256(api_key, b"gobby-managed-token-v1")`, survive daemon
restarts, and must be reissued when the bootstrap key rotates. The interactive
runtime challenge is answered by gdaemon using a fresh bootstrap read.

Web UI passwords are stored as salted Argon2id hashes in the canonical
`users.password_hash` column. Browser sessions store only token hashes and a
required `user_id`; deleting a user cascades to those sessions.

## Envelope Model

- Secret values in `secrets.encrypted_value` are encrypted with one random
  data-encryption key (DEK).
- The DEK is stored only as `secret_key_material.wrapped_dek`.
- `secret_key_material` stores KEK posture metadata needed to unwrap the DEK.
- Changing KEK posture re-wraps only the DEK. It must not rewrite
  `secrets.encrypted_value`.

## KEK Postures

| Posture | KEK Source | Hub Metadata | Runtime Requirement |
| --- | --- | --- | --- |
| `key_file` | `0600 ~/.gobby/.secret_kek` Fernet key | Wrapped DEK, posture | Local daemon can read the key file |
| `scrypt_passphrase` | Passphrase-derived scrypt key | Wrapped DEK, posture, scrypt salt and params | `GOBBY_SECRET_KEK_PASSPHRASE` or an interactive CLI prompt |

`key_file` is the default because it supports unattended daemon startup.
`scrypt_passphrase` is the passphrase opt-in, reached only through
`gobby secrets rekey --posture passphrase`; the installer always starts in
`key_file`.

## Canonical State

- A hub containing secret rows must contain the `default`
  `secret_key_material` row that wraps their DEK.
- Communications webhook secrets are stored in `SecretStore`; channel rows
  carry `$secret:NAME` references.
- Canonical user credentials live only in `users`; `auth.username`,
  `auth.password_hash`, and `auth.password` are not supported configuration
  keys.
