# Hub Install Contract

This contract defines CLI-visible hub adoption behavior for `gobby install` and related setup flows. The daemon owns schema creation and migration. The CLIs must preserve existing hub data and consume the resulting bootstrap configuration.

## Adoption Target

When install finds an existing reachable standalone hub recorded in `~/.gobby/gcore.yaml` at `databases.postgres.dsn`, and the matching `~/.gobby/services/` compose stack exists, it adopts that Postgres instance instead of creating a replacement hub.

Adoption means:

- Use the existing DSN as the daemon hub DSN.
- Apply the full Gobby schema additively in place.
- Preserve existing `code_*` and `gwiki_*` subset data.
- Preserve project identity rows; daemon project registration uses `ON CONFLICT DO NOTHING`.
- Write the adopted DSN into `~/.gobby/bootstrap.yaml`.

## Install Ordering

1. Read standalone config from `~/.gobby/gcore.yaml`.
2. If `databases.postgres.dsn` exists, connect to it before provisioning a new Postgres.
3. Confirm the hub is reachable and belongs to the local standalone service stack when `~/.gobby/services/` is present.
4. Classify the existing schema.
5. If the schema is adoptable, run additive daemon migrations in place.
6. Run the one-time embedding namespace migration from `embeddings.*` to `ai.embeddings.*`.
7. Provision `~/.gobby/local_cli_token` with mode `0600` and store its SHA-256
   digest as `auth.api_token_hash` when the hub is reachable.
8. Write `bootstrap.yaml` with the adopted Postgres DSN only after migrations succeed.
9. Leave the original standalone data intact for gcode and gwiki consumers.

Failed adoption must stop before destructive changes. It must not drop, truncate, or recreate existing subset tables.

## Additive Upgrade Rules

The daemon may create missing tables, indexes, extensions, functions, and default rows required by the full Gobby hub. It may add missing columns or constraints only when that operation preserves existing rows.

The daemon must not:

- Drop or truncate existing `code_*` tables.
- Drop or truncate existing `gwiki_*` tables.
- Rewrite `config_store` from the CLI side.
- Replace project rows that already identify the standalone hub's projects.
- Treat an otherwise valid `gwiki_*` subset as corrupt.

## Classifier Gap

The daemon baseline classifier currently recognizes standalone `code_*` tables but not standalone `gwiki_*` tables. A hub containing only or primarily `gwiki_*` subset data can be misclassified as `corrupt_partial`.

The daemon adoption classifier and migration skip-list must recognize both subsets:

- `code_*` tables are owned by gcode's code index projection.
- `gwiki_*` tables are owned by gwiki's wiki index projection.

Both subsets are adoptable inputs. Their data must survive the daemon full-schema upgrade.

## Bootstrap Output

After successful adoption, `~/.gobby/bootstrap.yaml` is the CLI source of truth
for the hub connection. It must point at the adopted Postgres instance,
advertise `hub_backend: postgres`, and carry the selected `auth_mode`. Missing
`auth_mode` resolves to `required`.

The previous `~/.gobby/gcore.yaml` entry can remain for standalone installs, but new daemon-aware CLIs should resolve the hub through `bootstrap.yaml`.

## Hub-Client Token Output

`gobby install` owns `$GOBBY_HOME/local_cli_token` (default
`~/.gobby/local_cli_token`) as part of the hub-client installation contract. A
database-unreachable install still creates the `0600` token file; first daemon
startup adopts its hash into `config_store`. Additional trusted client machines
receive an exact copy of the file with the same permissions.

`gobby auth token --rotate` replaces the hub token and stored hash. Operators
must recopy the file to every additional client machine after rotation.

## Embedding Namespace Migration

The hub upgrade flow also owns the one-time `config_store` migration from `embeddings.*` to `ai.embeddings.*`. This migration is daemon-owned because `config_store` is daemon-owned.

Embedding namespace invariants:

- Preserve values exactly.
- Preserve `is_secret`.
- Write embedding settings under `ai.embeddings.*`.
- Use `ai.embeddings.dim` for a configured embedding dimension.

See `ai-daemon-contract.md` for the full D6 writer and reader inventory.

_Last verified: 2026-07-10_
