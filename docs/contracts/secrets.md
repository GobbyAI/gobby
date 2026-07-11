# Secrets Contract

Gobby stores secret values in the hub. Decryption is limited to the daemon and
trusted local binaries that support standalone direct-hub mode. Remote clients
can create, replace, list metadata, and delete secrets; remote clients never
receive plaintext secret values, the DEK, or KEK material.

Daemon API tokens follow a separate one-way verification contract. They grant
daemon access and never participate in secret-envelope encryption.

## Daemon API Token

| Surface | Contract |
| --- | --- |
| Plaintext | `$GOBBY_HOME/local_cli_token` (default `~/.gobby/local_cli_token`), written with mode `0600` |
| Hub verifier | SHA-256 hex digest in `config_store` key `auth.api_token_hash` |
| Canonical HTTP credential | `Authorization: Bearer <token>` |
| Local alias | `X-Gobby-Local-Token: <token>` |
| Browser credential | `gobby_session` cookie created by `/api/auth/login` |

`gobby install` provisions the token. A file-only install is adopted into the
hub on daemon startup. When both values exist, the hub hash is authoritative;
a missing or mismatched file requires `gobby auth token --rotate` on the hub
machine and a fresh copy to every additional client machine.

Rotation replaces the plaintext file and stored hash together. Running clients
refresh within about five seconds. The old token stops authorizing HTTP and
direct WebSocket connections; browser sessions remain independent and the
browser WebSocket proxy reads the refreshed daemon token.

Web UI passwords are stored as salted scrypt hashes in `auth.password_hash`.
Daemon startup migrates a decryptable legacy `auth.password` secret to this
hash and removes the legacy value.

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
`scrypt_passphrase` is opt-in via `gobby install --secret-kek-posture passphrase`
or `gobby secrets rekey --posture passphrase`.

## Legacy Migration

Before envelope encryption, secret values were encrypted with a Fernet key
derived from local `machine_id` and `~/.gobby/.secret_salt`.

Migration rules:

- If `secret_key_material` is missing, the daemon attempts a one-time migration.
- Migratable legacy rows are decrypted with the current box's legacy key and
  re-encrypted with the new DEK.
- `gobby secrets migrate --dry-run` reports what would migrate without writing.
- Explicit `$secret:NAME` config-store references are startup-required. If one
  cannot be migrated, daemon startup fails.
- Legacy rows that are not required and cannot be migrated are logged with a
  non-reversible identifier and left for re-entry.
