# Secrets Contract

Gobby stores secret values in the hub, but decryption is daemon-only.
Clients can create, replace, list metadata, and delete secrets; clients never
receive plaintext secret values, the DEK, or KEK material.

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
