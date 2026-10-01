# API-key bootstrap replacing plaintext database credentials (#23128)

Research spike under #21542. It covers investigation only: no implementation, no live state change and no plan expansion. Evidence from closed #23106, including plan commit `0b0ba1fa9114121ca4ae086c7963303b1c814f36` and `.gobby/plans/gdaemon-api-keys-nodes.md`, is preserved and only cited here. All file references are to 0.5.0 at `9a514a7cf1`. No secret values appear anywhere in this document.

**Revision 2 (2026-10-01).** Josh replied to the first packet: "API keys would be valid only for the machine itself tied to. I don't want to use keychain. Other ideas? How do popular GitHub repos handle this?"

This revision makes these changes:
- Removes the OS credential store and Keychain (H3), which also removes the macOS signing fork.
- Makes client credentials machine-bound (section 5a).
- Adds a source-verified survey of popular self-hosted projects (section 11).
- Gives the hub's own Postgres password its own choice.

Superseded text is kept and marked.

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
- It leaves a client private-key file on disk, which is a bearer file of the same class as the password. With the OS store withdrawn (H3), that key file is protected only by its 0600 mode, so cert auth adds TLS mechanism without changing the exposure.

### H3. OS credential store (Keychain, Secret Service, DPAPI). Withdrawn, because Josh declined Keychain on 2026-10-01.

Josh: "I don't want to use keychain." The macOS ad-hoc-signing note applied only to a Keychain ACL, so it is withdrawn too. Its first-packet text is not repeated here; see commit `d6842a0`.

### H4. Seal under the existing SecretStore KEK. Still indirection.

`.secret_kek` is itself a 0600 plaintext key file, and memory 910e2822 names it the sole secret-material file. Sealing the DB password under it moves the password into the same surface class. With no OS store to hold the KEK, H4 adds mechanism without changing the exposure.

### H5. `systemd-creds`. Linux-only option.

`systemd-creds encrypt` uses a host key ("accessible only to root", `/var/lib/systemd/credential.secret`) and/or TPM2. User-scoped credentials need systemd **256+** (`--user`). Unattended start works. It does nothing for macOS hubs, which would still need H6.

### H1 revisited. Peer auth over a Unix socket, when it can work

- **macOS with Docker Desktop (today's macOS hub):** not viable.
  - Docker's archived Docker Desktop for Mac file-sharing docs (osxfs) state: "Socket files and named pipes only transmit between containers and between macOS processes -- no transmission across the hypervisor is supported, yet." ([docker.github.io osxfs.md](https://github.com/docker/docker.github.io/blob/a17a06c6a15028011250900c49a1b59620cf1c65/docker-for-mac/osxfs.md)).
  - The current virtiofs docs say nothing either way. A July 2024 user report shows a socket created in a bind mount failing with "Invalid argument" ([Docker forum](https://forums.docker.com/t/unix-socket-on-bind-mount/142653)).
  - Even if a socket did cross the VM, the Postgres server would see the peer credentials of a process on the VM side, not of the macOS user. That last point is an inference.
  - Status: **not viable, unverified on current Docker Desktop releases**. A live test would need a scratch container, which this task did not run.
- **Linux with Docker:** plausible. The container shares the host kernel, so a bind-mounted socket directory works. Peer then resolves the client uid *inside the container*, so the hub user's uid needs a passwd entry in the image, or a `pg_ident` map from a resolvable name. That second part is an inference from the [peer docs](https://www.postgresql.org/docs/current/auth-peer.html) and is unverified.
- **Native Postgres (Homebrew or system package):** the textbook case. Gobby currently **rejects** non-Compose Postgres (`config/bootstrap.py:180-183`), so this option means re-admitting native Postgres for hubs.
- **External hub:** the operator's `pg_hba` decides. Gobby can accept a socket DSN with no password.

### H6. One owner-only file, narrowed. Recommended.

The survey in section 11 shows this is the industry norm.
- Gitea keeps `[database] PASSWD` in `app.ini`.
- Grafana keeps `[database] password` in `grafana.ini`, or reads it from `GF_DATABASE_PASSWORD`.
- n8n reads `DB_POSTGRESDB_PASSWORD` and keeps its encryption key in a plaintext JSON settings file.
- Each of them adds only an indirection that moves the secret to another file or variable: Gitea's `*_URI = file:`, Grafana's `$__file{}`, n8n's `_FILE`.
- n8n alone enforces 0600 and repairs looser permissions.

Gobby already has the base shape: `database_url` in a 0600 `bootstrap.yaml`. The work is narrowing it to one copy:
1. The daemon is the only reader. gcode stops reading it (S6).
2. The Rust reader enforces 0600 the way Python does, and both repair looser permissions the way n8n does. That is fix task (b).
3. Compose receives the password only at initdb (S7).
4. On-disk grants drop their secret fields (S5, choice 8).
5. The rotation pair (S2) stays transient, as it is today.
6. Nodes never hold it (section 4).

This recommendation **does not meet the original "no plaintext DB password in files" goal** for the hub's own Postgres. It meets that goal for every node, and leaves the hub with exactly one 0600 copy. The threat baseline in section 2 applies: a same-user process with Docker access is already a superuser through container trust auth. Josh must accept that hub file explicitly (choice 1).

**Grant cache (S5).** Under H6 the hub keeps exactly one copy of a DB password on disk. The grant cache adds hundreds of 0600 files, 891 on this machine, each carrying a scoped-role password. The recommendation is to cache only non-secret grant metadata, and have gcode fetch `Direct` secrets per process through the existing loopback handshake. That costs one loopback handshake per gcode process. The alternative is HTTP-only gcode (D4), which also serves nodes. See choice 8.

## 4. Nodes: hub URL + machine key only, and the D2 prerequisite

**Node bootstrap (no option retains a DSN, password or KEK):**
- `hub_daemon_url`
- the pinned hub certificate fingerprint
- the node's own machine private key, as a 0600 file (section 5a)
- the machine id the hub assigned at enrollment

`datastore_mode: remote` DSN validation (`bootstrap.py:418-450`) and the S3/S4 copy steps are removed. Nodes never receive `.secret_kek`.

**Why D2 is a prerequisite, and why a "bounded subset" of storage is not smaller:**

- Today a node runs the whole Python daemon. Its storage layer, runner loops and lease all use psycopg directly. Run-modes Decision 5: "a node's runner talks to the hub database".
- A DSN-free node therefore needs one of two things:
  - **D2 thin relay** (`gdaemon-api-keys-nodes.md:984-1043`): a bare `gdaemon serve` that forwards bytes to the hub over one pinned connection, with no Python on the node.
  - An HTTP storage facade under every Python subsystem: sessions, tasks, memory, workflows, runner, lease. That is far larger than D2.
- P4 4.6 already refuses `datastore_mode: remote` starts. The honest interim is **no nodes until D2**, and #23269 (node refusal) is already in flight.

**Bounded D2 is possible, with one revocation requirement.** The prerequisite is the relay plus machine authentication on the hub. With machine-bound keys over mutual TLS (section 5a), the node is authenticated once per TLS connection, and the D2 relay holds one long-lived connection. Revocation therefore needs one of two things:
- The hub re-checks the machine's revoked state and epoch on every relayed request, keyed by the connection's verified public key.
- The hub closes the machine's live connections when the machine is revoked.

The per-request check keeps revocation correct without the `/api/nodes/channel` socket. That channel (hello/ack, ping, 4401 close) then only speeds up disconnects and reports presence, so it can follow the relay.

**D3 to D5 are functionality prerequisites, not credential prerequisites.**

- D3: a `gobby-mcp` crate, because a thin node has no Python MCP.
- D4: `gcode index` on a node needs hub HTTP routes.
- D5: hook envelopes must carry edited-file content.

A node without them is DSN-free but limited. The plan should say which of them gate "nodes supported".

## 5. First-run and enrollment

Revision 2 note: F2 and F3 now register a machine **public key** (section 5a) where the first packet registered a client-generated key hash. The concurrency and idempotence arguments are unchanged: a retry with the same public key is the same request.

| Option | Credential | Allowed operations | Exposure / replay | Concurrent first key | Failure / restart |
| --- | --- | --- | --- | --- | --- |
| **F0. Known shipped bootstrap secret** | Universal (same in every install) | Register the first machine | Public by construction: anything shipped to every install is known to every attacker who can reach the hub in the window | Race between any reachers | n/a. **Rejected** |
| **F1. P4 email/password route** (`POST /api/auth/keys/bootstrap`, :590-604) | User password, reusable | Mint a key for any machine, unlimited times | Reusable secret typed on remote machines; online brute force only rate-limited; replayable if a password leaks | Each call mints a key; no first-key notion | Stateless; retry mints duplicates |
| **F2. Limited first-run (Josh's idea)** | None. Authority is *local presence on the hub*: loopback peer **and** proof readable only by the hub OS user (the per-boot front-door secret or a one-time 0600 nonce) | Register exactly one machine, the hub's own, then lock | Loopback-only, so no network exposure. Same-user processes can win the window, which is the same trust as the DB baseline | The hub machine generates its keypair and sends only the public key. Registration is a singleton CAS (`hub_authority.state = unclaimed -> claimed`). Exactly one winner; a loser gets 409 | A lost response is retried with the **same** public key: equal key means idempotent success, never a lockout. Restart in the unclaimed state keeps the window. Restart after the claim keeps the lock |
| **F3. One-time enrollment code** (adding nodes) | Minted per enrollment; outcome is bound to one machine | Register one machine's public key, once | Code shown on the hub by an enrolled machine's user. Short TTL, single use, stored hashed, bound to the auth epoch. Transported over the pinned hub TLS | Consuming the code is a CAS; second use gets 409 | The node generates its keypair locally and sends code + public key. A retry with the same pair is idempotent until TTL. Expiry requires a new code |

Survey analogues (section 11):
- GitHub Actions runners register with a one-hour token, then authenticate with a locally generated RSA key whose public half the server holds.
- Tailscale enrolls with one-off or reusable auth keys and a device-generated machine key.
- Kubernetes kubelet TLS bootstrapping uses "a limited usage 'token'" to obtain a client certificate.

**Recommended: F2 for the hub's own machine, plus F3 for nodes. Drop F1.**

- F2 matches Josh's lock-after-first-key idea and needs no transmitted secret.
- F3 avoids putting the user's reusable password on remote machines.
- Idempotent registration by public key removes the "response lost, hub locked" failure mode.

## 5a. Machine-bound keys (revision 2)

Josh: "API keys would be valid only for the machine itself tied to."

| Option | How it binds | Mechanism cost | Verdict |
| --- | --- | --- | --- |
| **K1. Machine keypair + mutual TLS, public key pinned** | Each machine generates an Ed25519 keypair and a self-signed certificate. The hub stores the public key (SPKI hash) per machine. The front door requests a client certificate (optional mode, section 5a) and looks up the verified public key. This is Syncthing's model: device ID = "SHA-256 hash of the certificate data", and "Both devices present their certificates" | P4 4.1 already adds rustls TLS to the front door. K1 adds a custom client-cert verifier and one lookup. No CA key to protect, because certificates are pinned rather than CA-issued | **Recommended** |
| K2. Keypair + signed requests (HTTP Message Signatures, RFC 9421, or the Actions-runner signed-JWT pattern) | Per-request signature over method, path, date and nonce; the hub verifies with the stored public key | Custom canonicalization, plus a nonce/replay window, for every client in three languages (Python, Rust gcore/ghook, web) | Viable; more mechanism than K1 |
| K3. Bearer API key + `machine_id` | The hub records which machine a key belongs to | The header is client-asserted, so a copied key works from any machine | Rejected: the binding is nominal only |

What K1 gives:
- The private key never leaves the machine or crosses the wire.
- The hub database holds only public keys, so a hub DB leak yields no usable client credential.
- TLS prevents replay.
- Revocation and reset (section 6) set `revoked_at` on the machine's key row.

What "machine-bound" means without Keychain:
- The binding is **possession of the private-key file**. Copying that 0600 file to another machine moves the identity.
- Every surveyed project has the same default:
  - The Actions runner writes `.credentials_rsaparams` and `chmod 600` on Linux and macOS.
  - Syncthing writes `key.pem` with `0o600`.
  - WireGuard relies on `umask 077`.
  - Tailscale's file store is `0600`.
- The step up is hardware or OS sealing:
  - Tailscale's `--encrypt-state`, enabled "if supported on this platform", uses a TPM on Linux and Windows.
  - The Actions runner uses DPAPI on Windows.
- A TPM is distinct from Keychain and could be a later opt-in. It is not part of this recommendation.

### Revision 2a: credential per route and peer (PD review, 2026-10-01)

**Hub-local clients stay on loopback plaintext.** #23270 ("Front-door TLS for remote peers with loopback plaintext") keeps loopback in plaintext. A TLS handshake on every `ghook` call would tax the hook hot path. So for hub-local clients:
- Local authority = **loopback peer + the 0600 local proof** that F2 already uses (a hub-owner-readable file).
- K1 applies to **non-loopback peers only**.
- This replaces `local_cli_token` as P4 D1 (:858-982) intends. The local proof is the same kind of 0600 file, accepted only from loopback peers.
- The per-boot front-door secret stays the gdaemon-to-Python internal hop.

**Loopback alone is never authority.** Tailscale `serve --https` and Funnel terminate TLS in tailscaled and forward to the local service. The docs say "By default, the device's Tailscale daemon terminates the HTTPS connection" ([Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve)). This hub serves `https://mbp.tail4125a0.ts.net` to `localhost:60887`. Every tailnet request, and every Funnel request from the internet, therefore reaches the front door as a loopback peer. The local proof is what keeps those requests out of local authority. The same applies to the F2 first-run window and to reset.

**Web UI.** Browsers cannot practically present pinned client certificates. The web UI keeps cookie login: `users.password_hash` to `auth_sessions`, via `gobby auth credentials`. The front door therefore runs client certificates in **optional mode**. The TLS handshake succeeds without a client certificate, and the route layer decides what each connection may do. In rustls that is a client-certificate verifier that does not make client auth mandatory (`ClientCertVerifier::client_auth_mandatory`); the API is cited from docs.rs and not prototyped.

| Credential presented | Peer | Accepted on |
| --- | --- | --- |
| Local proof (0600 file) | Loopback only | All API routes, F2 first-run, `gobby auth reset` |
| Verified client certificate (K1), key not revoked, current epoch | Non-loopback, direct TLS to the front door | All API routes for that machine's user, D2 relay, issuing enrollment codes |
| Web session cookie (`auth_sessions`) | Any, including via Tailscale serve | Web UI pages and the API routes the web UI calls (same-origin, CSRF-protected as today), issuing enrollment codes. Never F2 or reset |
| Enrollment code (F3) | Any | `node join` only |
| None | Any | Health, the login form, the TLS certificate fingerprint |

**K1 and TLS-terminating proxies.** A client certificate cannot cross a proxy that terminates TLS.
- Under K1, remote machines dial the front door's own TLS listener directly, at its tailnet IP and port, and never the `serve --https` URL.
- `serve --tcp`, "a raw TCP forwarder", passes the client's TLS through intact, so K1 also works behind it. Such connections then arrive from loopback, and the front door must not treat them as loopback-local. They carry no local proof, so the table above already handles them.
- P4's hub-certificate pinning (`hub_cert`, 4.5) already requires a direct dial, because a serve URL presents tailscaled's certificate, not the hub's. K1 adds no new topology constraint.
- K2's signed requests do survive a terminating proxy. That is K2's real advantage (choice 10).

## 6. Reset-to-first-run

**Authorization.** Local presence on the hub (the same proof as F2) by default. Whether an enrolled machine may also trigger it remotely is a Josh choice; the recommendation is local-only. A remote reset cuts off every node, including the caller's, and offers no recovery path from off-host.

**One transaction:**

1. `hub_authority.epoch += 1` and state becomes `unclaimed`.
2. Set `revoked_at` on every live machine key, including the hub's own.
3. Delete outstanding enrollment codes.
4. Delete `auth_sessions`, which logs out the web UI.

Before the transaction commits, the front door still rejects any connection or request whose machine key is revoked or whose epoch is stale. Machine validation is one SQL query per request (section 4). If a validation cache is ever added, it must be keyed by epoch. When the channel exists, D2 channels close with 4401. The hub re-registers its own key through F2, and every node re-enrolls through F3. A node may keep its keypair, because re-enrollment registers the same public key under the new epoch, or generate a fresh one.

**Untouched:** users, tasks, sessions, memories, `secrets`, `machines` rows (marked unenrolled and kept for history), `.secret_kek`, and the DB password.

**Relation to other commands:**

- This is distinct from `gobby auth credentials`, which resets the web UI password (`cli/auth.py:23-30`).
- It is distinct from any data wipe.
- It is distinct from re-keying the hub DB password, which is a separate `gobby datastores rotate-password postgres` concern.

## 7. External hub and remote-node implications

- **Installed local hub (Docker Compose):** H6 (0600 file), plus H5 on Linux if chosen. Peer is not available on macOS Docker Desktop (section 3).
- **External hub** (Postgres not managed by Gobby):
  - The operator's `pg_hba` decides. Gobby should accept a socket (peer) or cert DSN with no stored password, and otherwise keep the password in the same single 0600 file.
- **Remote node:** bootstrap per section 4. Its machine private key is a 0600 file, with n8n-style enforce-and-repair of permissions. It holds no KEK and no DSN, and runs no Postgres, Qdrant or FalkorDB probes.
- **Unattended starts:**
  - With no OS store, nothing needs an unlock.
  - macOS LaunchAgent and Linux systemd user units start as they do today.
  - H5 works unattended on systemd 256+.

## 8. Plan sections and prerequisites that must change

1. `gdaemon-api-keys-nodes.md:53`: replace "a node's `database_url` is the hub's" with the DSN-free node bootstrap (section 4).
2. 4.2 schema:
   - Replace bearer `api_keys` (`key_hash`, `key_hint`, the `gobby_` format) with machine public keys: an SPKI hash per machine, plus `revoked_at` and `epoch`.
   - Add `hub_authority` (singleton state + epoch) and `enrollment_codes`.
   - Drop or keep the email/password bootstrap route (:590-604) per Josh's choice.
   - The migration number stays "next free after 455".
3. 4.1 TLS: **#23270 scope is unchanged by choice 10.**
   - #23270 builds server TLS, the first-byte peek and loopback plaintext, and all three stay.
   - If K1 is chosen, a separate follow-on leaf after #23270 adds the optional client-certificate verifier: pinned self-signed certificates checked against the machine table, with cert-less handshakes allowed for cookie and enrollment routes. It also adds the route-credential table from section 5a.
   - If K2 is chosen, 4.1 gains nothing; signature verification lives in the route layer.
   - Neither option needs L7 to change course on #23270.
4. 4.5:
   - Replace `gobby auth login` adding three fields to a remote bootstrap (:805, :755-761) with `gobby node join <hub-url> --code`. It generates the keypair and writes a fresh DSN-free bootstrap.
   - Add `gobby auth reset` (section 6) and the F2 first-run on the hub.
5. P4 D1 (`local_cli_token` sweep): hub-local clients present the 0600 local proof over loopback plaintext. The front door never treats a loopback peer alone as local authority, because of Tailscale serve and Funnel (section 5a).
6. 4.6 / #23269: the refusal stays. Nodes are "unsupported until D2".
7. Promote **D2 relay**, with per-request revocation checks and without the channel, to a prerequisite of any node support. State which of D3, D4 and D5 gate "nodes supported".
8. New hub-side slice (not in P4 today), "DB credential narrowing" (H6):
   - The daemon is the only reader of `database_url`. gcode S6 is removed.
   - The Rust reader enforces 0600, and both readers repair looser permissions.
   - Compose gets the password at initdb only.
   - The grant cache is secret-free (choice 8).
   - S10 `grant_signing_secret` is sealed under the existing SecretStore KEK, or kept in memory only. It is regenerated each lease epoch anyway (`daemon_lease.py:157-175`).
   - If choice 1 includes H5 or peer, the systemd-creds or socket DSN path is added.
   - It touches:
     - `config/bootstrap.py`
     - `cli/installers/postgres.py`
     - `compose_env.py`
     - `cli/datastores.py`
     - `crates/gcore/src/bootstrap.rs`
     - `crates/gcore/src/ai/effective_config.rs`
     - `crates/gcore/src/grant/cache.rs`
     - `daemon_lease.py`
9. `docs/guides/shared-stack.md` client setup: rewrite. Remove the DSN, KEK and token copies.
10. Run-modes: a `(remote, false)` node bootstrap has no `database_url`. Update the parser contract in both Python and Rust.

## 9. Found issues and PD dispositions

The PD disposed each finding on 2026-10-01:

- **(a) S6:** gcode reads the daemon's full bootstrap DSN (`effective_config.rs:212-222`). This contradicts `.gobby/plans/completed/daemon-native-runtime-boundary.md:281`. Belongs to the hub-side slice (section 8.8) and depends on choices 1 and 8.
- **(c) S10:** `grant_signing_secret` is stored plaintext in `deployment_runtime`. Belongs to the same slice and depends on choices 1 and 8.
- **(b) The Rust `read_hub_database_bootstrap_file` skips the 0600 mode check** (`bootstrap.rs:194-209`), and **(d) the top-level `~/.gobby/grants/` directory is 0755** (subdirectories 0700, files 0600). Both are independent and small. One fix task covers both, created after #23128 closes. Its gcore change ships in the next coherent-set release.
- **(e) The stray `falkordb_password` key** in this machine's bootstrap.yaml is unread by any parser. This is choice 9.

## 10. Choices for Josh (revision 2)

1. **Hub Postgres password at rest** (Keychain withdrawn):
   - **A. One 0600 file, narrowed (H6).** The Gitea, Grafana and n8n pattern. *Recommended.* It accepts one plaintext DB password file on the hub only; nodes hold none.
   - **B. Socket peer auth, where it works.** Linux Docker hubs (needs uid resolution inside the container, unverified), native Postgres (Gobby currently rejects non-Compose Postgres, `bootstrap.py:180-183`), or an external hub whose `pg_hba` allows it. Not available on macOS Docker Desktop: sockets do not cross the VM per Docker's archived docs, and the current docs are silent.
   - **C. Both.** Peer where available, otherwise A.
   - Also possible: H5 `systemd-creds` on Linux, with A on macOS. Cert auth (H2) only swaps the password for a key file.
2. *Withdrawn.* It asked what to do with no OS store, and that only applied to the Keychain option.
3. **Machine private key at rest** (nodes and the hub's own client key): 0600 file with enforce-and-repair (recommended; Syncthing, WireGuard and Actions-runner default), or also an opt-in TPM seal later (the Tailscale `--encrypt-state` pattern)?
4. **Node support timing:** accept "no nodes until D2 relay" (recommended), with per-request revocation checks and the channel following later?
5. **Which of D3 (MCP), D4 (gcode index) and D5 (hook file content)** must land before nodes count as supported?
6. **Reset authorization:** local-only (recommended), or also an enrolled machine remotely?
7. **P4's email/password bootstrap route:** drop in favour of enrollment codes (recommended), or keep as an alternative?
8. **Grant cache:** drop secret fields from the hundreds of on-disk grants and pay one loopback handshake per gcode process (recommended), accept them as 0600 files of the same class as the hub file, or move gcode to HTTP-only DB access (D4-style) for hubs too?
9. **Stray `falkordb_password` key** in the live `~/.gobby/bootstrap.yaml`: approve removing it. Nothing reads its value. The edit changes live state, so either Josh runs it or Josh approves an agent running it.
10. **Machine-bound mechanism** (applies to non-loopback machines; the hub's own clients use loopback + local proof, and browsers use cookie login):
    - **K1. Mutual TLS with pinned per-machine public keys.** The Syncthing model. *Recommended.*
      - Remote machines must dial the front door's TLS listener directly (tailnet IP:port, or `tailscale serve --tcp` passthrough), never a `serve --https` or Funnel URL, because those terminate TLS and drop the client certificate.
      - P4's hub-certificate pinning already requires that direct dial, so K1 adds no new constraint.
      - It costs one optional-client-cert verifier after #23270, with no per-request signing code in three client languages.
    - **K2. Per-request signatures** (the Actions-runner signed-JWT / RFC 9421 pattern).
      - Works through any TLS-terminating proxy, including `serve --https` and Funnel.
      - Costs canonical request signing plus a replay/nonce window in Python, Rust (gcore, ghook, gclient) and the relay. It also conflicts with P4's hub-certificate pin, unless the pin is dropped for proxied hubs.
    - Pick K2 only if nodes must reach the hub through a TLS-terminating proxy.

PD review of this revision is required before the Assistant presents the choices.

### Josh's decisions

**Pending.** Revision 2 (`b3023b3`) was presented on Telegram at 14:35 CT on 2026-10-01, and no choice is decided yet.

A decision table recorded in `4d8bc01` was **retracted**. The Assistant misread Josh's "Agreed", and Josh said: "I didn't accept yet."

## 11. Survey: how popular self-hosted projects handle this (verified)

Line numbers were counted against raw files at the cited release tags.

**The server's own DB password and app secrets: plaintext config, permission-protected, with file indirection.**

| Project | Evidence | Note |
| --- | --- | --- |
| Gitea v28.0.0 | `custom/conf/app.example.ini` L353 `[database]`, L367 `;PASSWD =`; L446 `SECRET_KEY =`, L450 `;SECRET_KEY_URI = file:/etc/gitea/secret_key`; L453 `INTERNAL_TOKEN =`, L456 `;INTERNAL_TOKEN_URI = file:...` | `*_URI = file:` moves a secret to its own file |
| Grafana v13.2.3 | `conf/defaults.ini` L223-233 `[database] … password =`; L466 `[security]`, L479-480 `secret_key = <shipped default>`, L483 `encryption_provider = secretKey.v1`. [Configure docs](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/): env `GF_<SECTION>_<KEY>`; the `$__file{}` provider "reads a file from the filesystem" | `secret_key` ships as a **fixed default**, not generated per install |
| n8n n8n@2.41.5 | `packages/core/src/instance-settings/instance-settings.ts` L65 settings file `~/.n8n/config`; L346-362 auto-generates the encryption key (`randomBytes(24)`); L387-388, L439-447 writes 0600 and repairs looser permissions. `packages/@n8n/config/src/configs/database.config.ts` L63 `DB_POSTGRESDB_PASSWORD`; `decorators.ts` L22-25 generic `_FILE` | The only one that enforces and repairs 0600 |
| Home Assistant | [secrets docs](https://www.home-assistant.io/docs/configuration/secrets/): "By using `!secret` you can remove any private information from your configuration files" | `secrets.yaml` is plain YAML |

**Enrolling remote machines: a short-lived or limited token, a device-generated keypair, the server keeps only the public half.**

| Project | Evidence |
| --- | --- |
| GitHub Actions runner v2.337.0 | `src/Runner.Listener/Configuration/ConfigurationManager.cs` L153 uses the registration token once; L206-208 `keyManager.CreateKey()`; L643-645 sends only `TaskAgentPublicKey(...)`. `src/Runner.Common/HostContext.cs` L474-477 key file `.credentials_rsaparams`. `OAuthCredential.cs` L47-49 authenticates with a signed-JWT client credential. [REST docs](https://docs.github.com/en/rest/actions/self-hosted-runners): "The token expires after one hour." At rest: `RSAFileKeyManager.cs` L23-33 plain + `chmod 600` on Linux/macOS; `RSAEncryptedFileKeyManager.cs` L74-75 DPAPI on Windows |
| Tailscale v1.102.5 | [auth keys](https://tailscale.com/kb/1085/auth-keys): "register new nodes without needing to sign in"; one-off, reusable, ephemeral, expiry 1-90 days. [Key management](https://tailscale.com/blog/tailscale-key-management): "first generates a curve25519 machine private key"; "The public component… is transmitted to the control server". At rest: `ipn/store/stores.go` L226 writes 0600; `cmd/tailscaled/tailscaled.go` L217, L980-983 `--encrypt-state` on by default where a TPM is supported |
| Syncthing v2.1.5 | [device IDs](https://docs.syncthing.net/dev/device-ids.html): "At first startup, Syncthing will create a public/private keypair"; the ID is the "SHA-256 hash of the certificate data in DER form"; "Both devices present their certificates". `lib/tlsutil/tlsutil.go` L176 writes `key.pem` 0600 |
| WireGuard | [wireguard.com](https://www.wireguard.com/): "Each network interface has a private key and a list of peers. Each peer has a public key." [Quickstart](https://www.wireguard.com/quickstart/): `umask 077` before `wg genkey` |
| Kubernetes kubelet | [TLS bootstrapping](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-tls-bootstrapping/): "a limited usage 'token'" with "limited credentials to create and retrieve a certificate signing request (CSR)" |

**Corrections to the examples first given to Josh:**
- Grafana's `secret_key` is a shipped default, not generated per install.
- n8n's `_FILE` support is generic, not specific to the DB password.
- Tailscale now TPM-encrypts its state by default where supported.
- Actions runners use DPAPI on Windows.
- Postgres peer auth does not reach a Docker Desktop hub on macOS (section 3).
