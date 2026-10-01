# API-key bootstrap replacing plaintext database credentials (#23128)

Research spike under #21542. It covers investigation only: no implementation, no live state change and no plan expansion. Evidence from closed #23106, including plan commit `0b0ba1fa9114121ca4ae086c7963303b1c814f36` and `.gobby/plans/gdaemon-api-keys-nodes.md`, is preserved and only cited here. All file references are to 0.5.0 at `9a514a7cf1`. No secret values appear anywhere in this document.

## 1. Two auth problems, one credential today

Gobby has two separate authentication problems:

- **(a) Client to hub API.** CLI, hooks, gcode, the web UI and remote nodes call the hub daemon over HTTP.
- **(b) Hub to PostgreSQL.** The daemon's psycopg and Rust pools log in to the database.

Today a remote node solves (a) by solving (b). It holds the hub's full Postgres DSN and copies `.secret_kek` and `local_cli_token` from the hub. Josh's goal separates them:

- A node holds only a hub URL and a minted API key, and moves all its data over HTTP.
- The hub reaches Postgres with no plaintext password in any file.

P4 as drafted still conflates the two. `gdaemon-api-keys-nodes.md:53` says "a node's `database_url` is the hub's". 4.5 `gobby auth login` adds `api_key`, `api_key_id` and `hub_cert` to an *existing* remote bootstrap (:805), and refuses mode changes on `database_url` grounds (:755-761). That is the api_key-beside-database_url shape Josh rejected.

## 2. Current plaintext credential surfaces (source inventory)

| # | Surface | Where | Class |
| --- | --- | --- | --- |
| S1 | `database_url` (hub superuser-equivalent DSN with password) in `~/.gobby/bootstrap.yaml` (0600) | writer `config/bootstrap_io.py:90-110`, `config/postgres_bootstrap.py:74+`; password minted `cli/installers/postgres.py:636-643` | file, hub |
| S2 | `credential_rotation.pending_password` / `previous_password` in bootstrap.yaml during a rotation | `cli/datastores.py:575-596` | file, hub |
| S3 | Node bootstrap carries the hub DSN; remote mode *requires* user+password and a non-loopback host | `config/bootstrap.py:418-450` (remote check :426-431); `docs/guides/shared-stack.md:60-111` | file, node |
| S4 | `.secret_kek` and `local_cli_token` scp'd to nodes | `shared-stack.md:69-94`; `cli/installers/remote_preflight.py:446-458` | file, node |
| S5 | Grant cache: Postgres `Direct{dsn,...}` with scoped-role password, FalkorDB password, Qdrant key | `crates/gcore/src/grant/bundle.rs:33-66`; cache `grant/cache.rs:49-60` (`~/.gobby/grants/<deployment>/<project>.json`, files 0600, dir 0700, top dir 0755); managed `grant.json` `runtime_grants/launch.py:26-55` | file, hub |
| S6 | gcode reads the bootstrap DSN directly (full daemon credential) for the effective-config revision check | `crates/gcore/src/ai/effective_config.rs:212-222` via `crates/gcode/src/config/layers.rs:647` | file read, hub |
| S7 | `GOBBY_POSTGRES_PASSWORD` derived from the DSN into the compose env, then container `POSTGRES_PASSWORD` (visible to `docker inspect`) | `cli/installers/compose_env.py:167-207`; `data/docker-compose.services.yml:83` | env/container config, hub |
| S8 | `GOBBY_DATABASE_URL` env to `gdaemon` schema commands | `storage/schema_contract.py:111`; `crates/gdaemon/src/main.rs:226-238` | env, hub |
| S9 | Backup-verify scratch container gets `-e POSTGRES_PASSWORD=<disposable>` on argv | `cli/hub_backup/_verify.py:153-154` | argv, hub, ephemeral |
| S10 | `deployment_runtime.grant_signing_secret` plaintext in the DB | `daemon_lease.py:157-175`; `baseline.sql:2406-2411` | DB row (not a DB password) |

Secondary facts:

- The Rust bootstrap reader `read_hub_database_bootstrap_file` (`crates/gcore/src/bootstrap.rs:194-209`) skips the 0600 check that Python enforces.
- This machine's bootstrap.yaml carries a stray `falkordb_password` key that no parser reads (key name only).
- SecretStore (Fernet DEK wrapped by `.secret_kek` or `GOBBY_SECRET_KEK_PASSPHRASE`, `storage/secrets.py:38-51,165-240`) never protects the main Postgres password.
- There is no OS keychain use anywhere. No `api_keys` table exists, and the newest migration is `455_drop_ask_artifacts.sql`.
- Postgres is Docker Compose only (`config/bootstrap.py:180-183`), published on `127.0.0.1:60891` by default (`docker-compose.services.yml:85`). It has no TLS and no `pg_hba` customization.
- The daemon runs as a per-user service: a macOS LaunchAgent (`cli/installers/service.py`, `~/Library/LaunchAgents`) or a systemd *user* unit (`cli/installers/service_linux.py:19-22`).

**Threat baseline worth stating.** The image grants trust auth to Unix-socket connections *inside* the container (Docker Hub `postgres`: "This user will be able to connect without a password due to the presence of trust authentication for Unix socket connections made inside the container"). Anyone who can `docker exec` on the hub is therefore already a database superuser without the password. Hiding the hub's password protects against **file exfiltration**: backups, `gobby pack` archives, sync tools, and coding agents reading `~/.gobby` files. It also protects against other local OS users reaching the loopback TCP port. It does not protect against a compromised process running as the hub's own OS user with Docker access. Every option below is judged against that baseline.

## 3. Hub to Postgres: options without a plaintext password in files

Primary sources:

- PostgreSQL 18 docs: [peer](https://www.postgresql.org/docs/current/auth-peer.html), [cert](https://www.postgresql.org/docs/current/auth-cert.html), [pg_hba](https://www.postgresql.org/docs/current/auth-pg-hba-conf.html), [pgpass](https://www.postgresql.org/docs/current/libpq-pgpass.html), [socket settings](https://www.postgresql.org/docs/current/runtime-config-connection.html).
- Docker Hub [`postgres`](https://hub.docker.com/_/postgres).
- macOS `security(1)`, and Apple [TN2083 daemons and agents](https://developer.apple.com/library/archive/technotes/tn2083/_index.html).
- freedesktop [Secret Service unlocking](https://specifications.freedesktop.org/secret-service/latest/unlocking.html).
- [`systemd-creds(1)`](https://man7.org/linux/man-pages/man1/systemd-creds.1.html).
- Microsoft [`CryptProtectData`](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).

### H1. Peer auth over a Unix socket. Rejected as the primary path.

Peer "works by obtaining the client's operating system user name from the kernel" and "is only supported on local connections"; it is available on Linux, BSD and macOS. Three problems rule it out here:

- Our server runs in a container. The daemon would need the container's socket directory bind-mounted to the host.
- The server resolves the peer uid inside the *container's* user namespace, so the hub's uid needs a passwd entry or a map there. That is an inference from the docs and is unverified.
- On Docker Desktop (macOS, Windows) the server runs in a Linux VM. Docker's file-sharing docs say nothing about Unix sockets crossing that boundary, so socket passthrough is **unverified**.

Peer is at best a Linux-only variant and cannot be the cross-platform default. It is also unusable for an external hub, since it is local-only.

### H2. Client-certificate auth. Meets the letter of the goal, but rejected.

Cert auth sends no password ("No password prompt will be sent to the client") and requires SSL. Two costs:

- It needs TLS added to the compose Postgres.
- It leaves a client private-key file on disk, which is a bearer file of the same class as the password. Protecting the key needs the same OS store as H3, so it adds TLS mechanism without removing the root secret.

### H3. SCRAM password at rest in the OS credential store. Viable.

Per platform:

- **macOS:** login keychain generic password (`security add-generic-password` / `find-generic-password`). The item ACL can be limited to the signed `gdaemon` binary through the trusted-app list and partition list (`security(1)`, `set-generic-password-partition-list`: "limits access to the item based on an application's code signature"). TN2083 puts agents in a per-user security context and daemons in the global one. Inference: the login keychain is reachable from the LaunchAgent once the user has logged in, which matches today's install, and not before login.
- **Linux desktop:** Secret Service. "The secrets of locked items cannot be accessed", and unlock may need a prompt. A lingering `systemd --user` unit started at boot has no unlocked collection, so this backend fails unattended start.
- **Linux headless or unattended:** `systemd-creds encrypt` with a host key ("accessible only to root") and/or TPM2. User-scoped credentials need systemd **256+** (`--user`). Older systems have no protected backend.
- **Windows:** DPAPI. Data is decryptable only by "a user with logon credentials that match", on the same computer, with no prompt when `CRYPTPROTECT_UI_FORBIDDEN` is set.

### H4. Seal under the existing SecretStore KEK. This is indirection on its own.

`.secret_kek` is itself a 0600 plaintext key file, and memory 910e2822 names it the sole secret-material file. Sealing the DB password under it alone moves the password into the same surface class.

### Recommended: H3 + H4 combined, one root secret

1. Put the **KEK** in the OS credential store (H3 backends). This retires the `.secret_kek` file on hubs whose backend is available. `GOBBY_SECRET_KEK_PASSPHRASE` stays the headless path, sourced from a `systemd-creds` credential.
2. Store the Postgres password, any rotation pending/previous pair (S2), and the FalkorDB/Qdrant datastore passwords **sealed by that KEK** in bootstrap.yaml.
   - The DB password cannot live in the DB's own `secrets` table, because it is needed to open the DB.
   - bootstrap.yaml keeps host, port, user and dbname only.
3. The installer passes the password to Compose only for **initdb**. The image's `POSTGRES_*` variables "only have an effect if you start the container with a data directory that is empty". Later `up` runs omit it (S7). Rotation already happens through SQL.
4. Use one OS-store reader. Only `gdaemon` reads the KEK from the OS store, and the macOS item ACL trusts only `gdaemon`.
   - Python cannot be the reader. Its interpreter is not Gobby-signed, so an ACL naming it would trust every Python script.
   - An ACL that trusts `/usr/bin/security` trusts any caller of that tool, so it is excluded too.
   - The Python runner already spawns `gdaemon` (`runner_front_door.py::FrontDoorChild`). It receives the KEK over the inherited `GOBBY_PARENT_FD` channel, never through env or a file, and unseals the DB password in memory.
   - One-shot Python CLI commands that need the KEK while no daemon is running (`gobby datastores`, `unpack`) get it from a `gdaemon` subcommand over a stdout pipe.
   - Linux and Windows backends have no code-signature ACL. The same single-reader shape is kept anyway, for one code path.
   - `GOBBY_DATABASE_URL` (S8) carries the DSN only in a child env, never in a file, which is acceptable under the goal. gcode's direct bootstrap read (S6) moves onto the grant path.

Pros:

- One root secret per hub.
- It reuses existing SecretStore code.
- The `.secret_kek` file disappears, which closes the KEK-copying route too.
- At-rest exfiltration of `~/.gobby` no longer yields any DB password.

Cons:

- Unattended start depends on a platform backend: macOS before login, Linux below systemd 256 without TPM or host key, and container-hosted hubs.
- Recovery when the OS store is lost (new machine, reset keychain) needs a re-key path. See section 6.
- A process running as the same OS user can still ask the store. On macOS the code-signature ACL narrows that. Linux and Windows backends do not.
- **macOS: ad-hoc signing breaks the ACL on every rebuild.** `promote_workspace_binary_set` (`src/gobby/install/bin_set_coherence.py`) ad-hoc signs each staged binary. An ad-hoc signature's designated requirement is its cdhash, so a trusted-app or partition-list entry bound to `gdaemon` stops matching after every `gdaemon` rebuild. After each crate release the result is a keychain prompt, or a failed unattended start. There are two fixes:
  - Sign with a stable **Developer ID** identity, so the designated requirement survives rebuilds.
  - **Re-ACL at install/promote time**, after each promotion. That step itself needs the keychain password, which makes promotion interactive.

**Grant cache (S5).** Under the goal, on-disk grants must stop carrying secret fields. The recommendation is to cache only non-secret grant metadata and have gcode fetch `Direct` secrets per process through the existing loopback handshake. That costs one loopback handshake per gcode process. The alternative is HTTP-only gcode (D4), which also serves nodes.

## 4. Nodes: hub URL + key only, and the D2 prerequisite

**Node bootstrap (no option retains a DSN, password or KEK):** `hub_daemon_url`, an API key (or an OS-store reference to it), `api_key_id`, and the pinned hub certificate fingerprint. `datastore_mode: remote` DSN validation (`bootstrap.py:418-450`) and the S3/S4 copy steps are removed. Nodes never receive `.secret_kek`.

**Why D2 is a prerequisite, and why a "bounded subset" of storage is not smaller:**

- Today a node runs the whole Python daemon. Its storage layer, runner loops and lease all use psycopg directly. Run-modes Decision 5: "a node's runner talks to the hub database".
- A DSN-free node therefore needs one of two things:
  - **D2 thin relay** (`gdaemon-api-keys-nodes.md:984-1043`): a bare `gdaemon serve` that forwards bytes to the hub over one pinned connection, with no Python on the node.
  - An HTTP storage facade under every Python subsystem: sessions, tasks, memory, workflows, runner, lease. That is far larger than D2.
- P4 4.6 already refuses `datastore_mode: remote` starts. The honest interim is **no nodes until D2**, and #23269 (node refusal) is already in flight.

**Bounded D2 is possible.** The prerequisite is the relay plus key authentication on the hub. The `/api/nodes/channel` socket (hello/ack, ping, 4401 close on revoke) is needed only for prompt disconnects and machine presence. Revocation stays correct without it, because the hub checks the key on every relayed request. So D2's channel can follow the relay.

**D3 to D5 are functionality prerequisites, not credential prerequisites.**

- D3: a `gobby-mcp` crate, because a thin node has no Python MCP.
- D4: `gcode index` on a node needs hub HTTP routes.
- D5: hook envelopes must carry edited-file content.

A node without them is DSN-free but limited. The plan should say which of them gate "nodes supported".

## 5. First-run and enrollment

| Option | Credential | Allowed operations | Exposure / replay | Concurrent first key | Failure / restart |
| --- | --- | --- | --- | --- | --- |
| **F0. Known shipped bootstrap secret** | Universal (same in every install) | Mint first key | Public by construction: anything shipped to every install is known to every attacker who can reach the hub in the window | Race between any reachers | n/a. **Rejected** |
| **F1. P4 email/password route** (`POST /api/auth/keys/bootstrap`, :590-604) | User password, reusable | Mint a key for any machine, unlimited times | Reusable secret typed on remote machines; online brute force only rate-limited; replayable if a password leaks | Each call mints a key; no first-key notion | Stateless; retry mints duplicates |
| **F2. Limited first-run (Josh's idea)** | None. Authority is *local presence on the hub*: loopback peer **and** proof readable only by the hub OS user (the per-boot front-door secret or a one-time 0600 nonce) | Mint exactly one key, the hub owner's first, then lock | Loopback-only, so no network exposure. Same-user processes can win the window, which is the same trust as the DB baseline | Client generates the key; server stores only its hash with a singleton CAS (`hub_authority.state = unclaimed -> claimed`). Exactly one winner; a loser gets 409 | A lost response is retried with the **same** key: equal hash means idempotent success, never a lockout. Restart in the unclaimed state keeps the window. Restart after the claim keeps the lock |
| **F3. One-time enrollment code** (adding nodes) | Minted per enrollment, machine-specific outcome | Register one machine and mint its key, once | Code shown on the hub by an authenticated key holder. Short TTL, single use, stored hashed, bound to the auth epoch. Transported over pinned TLS | Consuming the code is a CAS; second use gets 409 | The node generates its key locally and sends code + key hash. A retry with the same pair is idempotent until TTL. Expiry requires a new code |

Resulting keys are **minted and machine-specific** (`api_keys.machine_id`). With no scopes column, per ROADMAP decision 17, a key carries its user's full authority. Only the bootstrap credentials are narrowed: F2 can mint one first key, and F3 can enroll one machine.

**Recommended: F2 for the hub's own first key, plus F3 for nodes. Drop F1.**

- F2 matches Josh's lock-after-first-key idea and needs no transmitted secret.
- F3 avoids putting the user's reusable password on remote machines.
- The idempotent client-generated-key CAS removes the "response lost, hub locked" failure mode.

## 6. Reset-to-first-run

**Authorization.** Local presence on the hub (the same proof as F2) by default. Whether an existing admin key may also trigger it remotely is a Josh choice; the recommendation is local-only. A remote reset kills every node, including the caller's, and offers no recovery path from off-host.

**One transaction:**

1. `hub_authority.epoch += 1` and state becomes `unclaimed`.
2. Set `revoked_at` on every live key.
3. Delete outstanding enrollment codes.
4. Delete `auth_sessions`, which logs out the web UI.

Before the transaction commits, the front door still rejects any key whose row is revoked or whose epoch is stale. Key validation is already one SQL query per request (P4 D1, :868-883). If a validation cache is ever added, it must be keyed by epoch. When the channel exists, D2 channels close with 4401. Every node must re-enroll through F3.

**Untouched:** users, tasks, sessions, memories, `secrets`, `machines` rows (marked unenrolled and kept for history), the KEK, and the DB password.

**Relation to other commands:**

- This is distinct from `gobby auth credentials`, which resets the web UI password (`cli/auth.py:23-30`).
- It is distinct from any data wipe.
- It is distinct from re-keying the hub DB password, which is a separate `gobby datastores rotate-password postgres` concern.
- **OS-store loss** (section 3) is recovered by a local re-key: the operator supplies the DB superuser path through `docker exec` (container trust), and the hub sets a new password, seals it and stores a new KEK. Secrets sealed under the lost KEK are unrecoverable unless the passphrase path was used. That loss is a property of envelope encryption, not of this design.

## 7. External hub and remote-node implications

- **Installed local hub:** sections 3 and 5 apply directly.
- **External hub** (Postgres not managed by Gobby):
  - The DB password still needs at-rest protection. H3 applies unchanged.
  - Peer and cert depend on the external server's `pg_hba`, which the operator owns. Gobby should support whatever DSN shape they configure, without a stored password, when the DSN uses a socket or cert.
- **Remote node:** bootstrap per section 4. No KEK, no DSN, and no Postgres, Qdrant or FalkorDB probes.
  - The node's API key is a bearer secret for the hub API. Whether it may sit in a 0600 file or must use the OS store is a Josh choice. The recommendation is OS store when available and a 0600 file otherwise. That is consistent with the goal, which concerns DB credentials.
- **Unattended starts:**
  - macOS hubs start at login (LaunchAgent) and are fine.
  - Linux hubs need systemd 256+ or a TPM.
  - Nodes need only their API key, so the same backend rules apply to the key.

## 8. Plan sections and prerequisites that must change

1. `gdaemon-api-keys-nodes.md:53`: replace "a node's `database_url` is the hub's" with the DSN-free node bootstrap (section 4).
2. 4.2 schema:
   - Add `hub_authority` (singleton state + epoch) and `enrollment_codes`.
   - Store the client-generated key hash.
   - Drop or keep the email/password bootstrap route (:590-604) per Josh's choice.
   - The migration number stays "next free after 455".
3. 4.5:
   - Replace `gobby auth login` adding three fields to a remote bootstrap (:805, :755-761) with `gobby node join <hub-url> --code`. It writes a fresh DSN-free bootstrap.
   - Add `gobby auth reset` (section 6) and the F2 first-run on the hub.
4. 4.6 / #23269: the refusal stays. Nodes are "unsupported until D2".
5. Promote **D2 relay** (without channel) to a prerequisite of any node support. State which of D3, D4 and D5 gate "nodes supported".
6. New hub-side slice (not in P4 today), "DB credential at rest": KEK in the OS store read only by `gdaemon` and handed to Python over `GOBBY_PARENT_FD`, sealed DB/datastore passwords in bootstrap, init-only Compose password, gcode S6 removal, secret-free grant cache, and S10 `grant_signing_secret` sealed under the same KEK in `deployment_runtime`. It touches:
   - `config/bootstrap.py`
   - `cli/installers/postgres.py`
   - `compose_env.py`
   - `cli/datastores.py`
   - `crates/gcore/src/bootstrap.rs`
   - `crates/gcore/src/ai/effective_config.rs`
   - `crates/gcore/src/grant/cache.rs`
   - `storage/secrets.py`
   - `daemon_lease.py`
   - `runner_front_door.py`
7. `docs/guides/shared-stack.md` client setup: rewrite. Remove the DSN, KEK and token copies.
8. Run-modes: a `(remote, false)` node bootstrap has no `database_url`. Update the parser contract in both Python and Rust.

## 9. Found issues and PD dispositions

The PD disposed each finding on 2026-10-01:

- **(a) S6:** gcode reads the daemon's full bootstrap DSN (`effective_config.rs:212-222`). This contradicts `.gobby/plans/completed/daemon-native-runtime-boundary.md:281`. Belongs to the "DB credential at rest" slice (section 8.6) and depends on choices 1 and 8.
- **(c) S10:** `grant_signing_secret` is stored plaintext in `deployment_runtime`. Belongs to the same slice and depends on choices 1 and 8.
- **(b) The Rust `read_hub_database_bootstrap_file` skips the 0600 mode check** (`bootstrap.rs:194-209`), and **(d) the top-level `~/.gobby/grants/` directory is 0755** (subdirectories 0700, files 0600). Both are independent and small. One fix task covers both, created after #23128 closes. Its gcore change ships in the next coherent-set release.
- **(e) The stray `falkordb_password` key** in this machine's bootstrap.yaml is unread by any parser. This is choice 9.

## 10. Choices for Josh

1. **Where the hub DB password rests.** Recommended: KEK in the OS store read only by `gdaemon`, with the DB password sealed in bootstrap (section 3). Alternatives: Linux-only peer (H1, unverified); a cert key file (H2).
   - On macOS this choice also needs a signing decision, because ad-hoc signing breaks the keychain ACL on every `gdaemon` rebuild.
   - Option A: a Developer ID signing identity. It needs an Apple Developer account, and the release pipeline must sign with it.
   - Option B: re-ACL at each install/promote. Every promotion then needs the keychain password.
   - Neither option means a prompt or an unattended-start failure after each crate release.
2. **No protected backend available** (old Linux, no TPM, containerized hub): refuse to start, or allow an explicit opt-in 0600 file with a warning?
3. **Node API key at rest:** OS store with a 0600-file fallback (recommended), OS store only, or 0600 file only?
4. **Node support timing:** accept "no nodes until D2 relay" (recommended), with D2's channel following later?
5. **Which of D3 (MCP), D4 (gcode index) and D5 (hook file content)** must land before nodes count as supported?
6. **Reset authorization:** local-only (recommended), or also an existing admin key remotely?
7. **P4's email/password bootstrap route:** drop in favour of enrollment codes (recommended), or keep as an alternative?
8. **Grant cache:** drop secrets from on-disk grants and pay one loopback handshake per gcode process (recommended), or move gcode to HTTP-only DB access (D4-style) for hubs too?
9. **Stray `falkordb_password` key** in the live `~/.gobby/bootstrap.yaml`: approve removing it. Nothing reads its value. The edit changes live state, so either Josh runs it or Josh approves an agent running it.

PD review of this packet is required before the Assistant presents the choices.
