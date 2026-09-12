# gobby-core Development Guide

Technical internals for developers and agents working in the `gobby-core` crate (`crates/gcore/`).

## What gobby-core Is

`gobby-core` is the shared Rust foundation crate for Gobby CLI crates and future Rust daemon work. It holds the boring, reusable platform layer: project discovery, bootstrap and daemon addressing, shared context/config contracts, grant handshake, degradation vocabulary, feature-gated datastore adapters, and generic indexing/search primitives.

Domain behavior stays out of this crate. Code graph facts, symbol IDs, language parsing policy, task behavior, memory behavior, and CLI output formatting belong to consumer crates.

The baseline crate remains dependency-light. Consumers that only need project discovery and daemon helpers do not inherit PostgreSQL, FalkorDB, Qdrant, reqwest, ignore, or sha2 unless they opt in through Cargo features.

## Module Map

`crates/gcore/src/`:

| Module | Feature | Responsibility |
|--------|---------|----------------|
| `project` | always | Walk up from a starting directory to find a `.gobby/` directory containing `project.json` or `gcode.json`. Read the `id` field from the project identity file. |
| `bootstrap` | always | Read `~/.gobby/bootstrap.yaml` to get the daemon's listen endpoint (`bind_host`, `daemon_port`). Falls back to `127.0.0.1:60887` when the file is missing or malformed. |
| `daemon_url` | always | One daemon-URL resolver for all binaries: `GOBBY_DAEMON_URL` → `GOBBY_PORT` → bootstrap endpoint, normalizing wildcard listen addresses (`0.0.0.0`, `::`, `::0`) to `127.0.0.1` and bracketing bare IPv6 literals. |
| `grant` | always | Signed grant handshake, cache, and typed grant errors for daemon-native clients. |
| `config` | always | Shared configuration-resolution contracts. Environment variables, `config_store`, and defaults are represented here as the foundation expands. |
| `ai_context`, `ai_types` | always | Shared AI context and serializable type contracts; consumer runtime contexts remain in their own crates. |
| `cli_contract`, `database_concurrency` | always | Shared CLI contract and database concurrency primitives. |
| `local_token`, `machine` | always | Local client credential and machine identity helpers. |
| `markdown`, `mermaid`, `progress` | always | Shared presentation parsing and progress primitives. |
| `degradation` | always | Shared vocabulary for configured-service unavailability, explicit degraded paths, partial search, stale indexes, skipped artifacts, and fatal core errors. |
| `ai` | `ai` | Shared AI routing, daemon transports, profile tiers, embeddings, and agentic/tool-loop generation primitives. |
| `schema` | `postgres` | Hub schema apply/verify authority. Runtime commands validate externally managed resources and do not implicitly migrate them. |
| `token_budget` | always | Shared token-budget trimming helpers — bounds prompt/context payloads to a token ceiling so consumers reuse one budgeting primitive. |
| `postgres` | `postgres` | PostgreSQL hub adapter boundary. Validates Gobby-owned schema and BM25 requirements without creating, altering, or dropping managed objects. |
| `falkor` | `falkor` | FalkorDB adapter boundary. Graph connection helpers live here without making FalkorDB a baseline dependency. |
| `qdrant` | `qdrant` | Qdrant adapter boundary for vector search/storage integration. |
| `indexing` | `indexing` | Generic file walking, hashing, and indexing primitives that are not tied to one domain model. |
| `search` | `search` | Generic search result and fusion primitives. Domain-specific search behavior stays in consumers. |
| `graph_analytics` | `graph-analytics` | In-memory graph analytics — weighted Leiden community detection over a consumer-supplied graph. No optional dependency; gated so the public surface stays explicit. |

Feature-gated modules are part of the public module map but compile only when their feature is selected.

## Public API

### `project`

```rust
pub fn find_project_root(start: &Path) -> Option<PathBuf>;
pub fn read_project_id(project_root: &Path) -> anyhow::Result<String>;
```

`find_project_root` walks up from `start` looking for a `.gobby/project.json` (Gobby-managed) or `.gobby/gcode.json` (gcode-standalone). Returns the directory *containing* `.gobby/`, not `.gobby/` itself. Returns `None` when neither marker is found before hitting the filesystem root.

`read_project_id` reads `<root>/.gobby/project.json` and extracts the string `id`
field. If that file is absent or invalid, it tries an existing
`<root>/.gobby/gcode.json`. Failure of both reads is an error. This low-level
fallback is not checkout registration: current gcode runtime resolution rejects
a root carrying only standalone `gcode.json` with `checkout_required`.

```rust
let cwd = std::env::current_dir()?;
if let Some(root) = gobby_core::project::find_project_root(&cwd) {
    let id = gobby_core::project::read_project_id(&root)?;
    println!("project {id} at {}", root.display());
}
```

### `bootstrap`

```rust
pub const DEFAULT_DAEMON_PORT: u16 = 60887;
pub const DEFAULT_BIND_HOST: &str = "127.0.0.1";

pub struct DaemonEndpoint {
    pub daemon_url: Option<String>,
    pub host: String,
    pub port: u16,
}

pub fn bootstrap_path() -> Option<PathBuf>;
pub fn read_daemon_endpoint() -> DaemonEndpoint;
pub fn read_daemon_endpoint_at(path: &Path) -> DaemonEndpoint;
```

`read_daemon_endpoint` is the lookup callers want. `read_daemon_endpoint_at`
accepts an explicit path for tests. Missing/unreadable files and malformed YAML
return defaults; individual absent or invalid fields use their own defaults,
preserving valid siblings. The optional full `daemon_url` is preserved for the
URL resolver. These readers do not surface errors.

`DaemonEndpoint` returns the raw endpoint as written. `0.0.0.0` and `::` are valid listen addresses but invalid dial addresses — normalization is the caller's job, or the `daemon_url` module's, not this one's.

### `daemon_url`

```rust
pub fn daemon_url() -> String;
pub fn daemon_url_at(path: &Path) -> String;
```

The one daemon-URL resolver every Gobby binary shares. `daemon_url()` applies a single env-override contract before reading bootstrap:

1. `GOBBY_DAEMON_URL` — full base-URL override; trailing slashes are trimmed, empty values ignored.
2. `GOBBY_PORT` — port-only override, dialed as `http://127.0.0.1:{port}`; empty or unparseable values ignored.
3. `GOBBY_DAEMON_PORT` — retained port alias after `GOBBY_PORT`.
4. Nonempty bootstrap `daemon_url`, with trailing slashes trimmed.
5. Bootstrap host/port endpoint, with dial normalization.

`daemon_url_at(path)` reads a specific bootstrap file and never consults the env — an explicit path is already an override.

Host/port dial normalization rewrites wildcard listen hosts (`0.0.0.0`, `::`,
`::0`, `[::]`), empty hosts and case-insensitive `localhost` to `127.0.0.1`, and
brackets bare IPv6 literals (`::1` → `[::1]`). Other hostnames and explicit IPv4
literals pass through. A full URL override is not rewritten through this helper.

```rust
let url = gobby_core::daemon_url::daemon_url();
// "http://127.0.0.1:60887" for default bootstrap
// "http://10.0.0.5:61234" if bootstrap has bind_host: 10.0.0.5
// "http://127.0.0.1:60887" if bootstrap has bind_host: 0.0.0.0
// GOBBY_DAEMON_URL / GOBBY_PORT win over all of the above
ureq::post(&format!("{url}/api/hooks/execute")).send_string(body)?;
```

### `grant`

Clients acquire a signed grant before constructing feature services. There is
one runtime: daemon-granted. Unresolved secret markers that reach a client are
grant-issuance bugs and fail typed.

### `falkor`

```rust
impl GraphClient {
    pub fn from_config(config: &FalkorConfig, graph_name: &str) -> anyhow::Result<Self>;
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>>;
}
```

Consumers supply the graph name through `GraphClient::from_config`;
`gobby-core` must not hardcode code or memory graph defaults. Connection fields
stay private. Use `query` for Cypher reads/writes and the explicit node or
relationship index helpers for index setup. The adapter uses Redis protocol
commands with bounded socket/query timeouts and typed compact-result parsing;
the former `with_sync_graph` escape hatch no longer exists. Unavailable graph
service is an error, not an empty successful result.

### `degradation`

```rust
pub enum ServiceState;
pub struct SetupIssue;
pub struct Guidance;
pub enum CoreError;
pub enum DegradationKind;
```

`degradation` defines the shared vocabulary for fatal core failures and non-fatal partial results. `ServiceState` travels with adapter results so callers can distinguish an available service, a service with no configuration, and a configured service that is unreachable. `CoreError` is reserved for command-stopping failures such as invalid configuration, unavailable required services, failed writes, and corrupted input.

`DegradationKind` is for successful operations that returned less than the ideal result. A `gobby-code` search can return symbol or content results while marking a configured Qdrant or FalkorDB outage as a `ServiceUnavailable` degradation. It can also report `PartialSearch`, `StaleIndex`, or `SkippedArtifacts` without converting those states into fatal CLI errors.

`Guidance` and `SetupIssue` carry structured remediation. Consumer CLIs render the `problem`, `action`, and optional `command_hint` fields in their own output style; `gobby-core` provides the shared contract.

## Boundary Rules

Each module exists because multiple Rust consumers need the same infrastructure contract, and getting it slightly wrong in one crate would silently misbehave.

| Boundary | Consumers | What stays out |
|----------|-----------|----------------|
| Project/bootstrap/daemon helpers | `gcode`, `ghook`, future Rust consumers | CLI rendering, command dispatch, daemon workflow semantics. |
| Context/config/degradation contracts | `gcode`, daemon work | Domain-specific flags, output formats, and task/memory behavior. |
| Datastore adapters | Consumers that opt in to `postgres`, `falkor`, or `qdrant` | Schema ownership, migrations, code graph facts, vector content policy. |
| Indexing/search primitives | Consumers that opt in to `indexing` or `search` | Code symbol IDs, language parsing policy, document models, ranking UX. |

`gobby-core` can validate externally managed resources, but it must not create, alter, drop, or migrate Gobby-owned resources during normal runtime commands.

## Feature Gates

The crate's default feature set is empty:

```toml
[features]
default = []
postgres = ["dep:postgres", "dep:postgres-openssl", "dep:base64", "dep:scrypt", "dep:sha2", "dep:time"]
falkor = ["dep:redis"]
qdrant = ["dep:reqwest", "dep:urlencoding"]
indexing = ["dep:ignore", "dep:sha2"]
search = []
graph-analytics = []
ai = ["dep:reqwest", "dep:base64", "dep:bytes", "dep:httpdate", "dep:rand", "reqwest/multipart"]
full = ["postgres", "falkor", "qdrant", "indexing", "search", "graph-analytics", "ai"]
```

`openssl` is intentionally **not** feature-gated. The crate declares
`openssl = { version = "0.10", features = ["vendored"] }` as a non-optional
dependency so `openssl-sys` stays a static (vendored) build across all
consumers and targets — including graphs that omit `postgres` (ghook, gcode's
build-dependency graph) — which the Windows release runners require because
they have no system OpenSSL.

Feature rationale:

| Feature | Enables | Why gated |
|---------|---------|-----------|
| `postgres` | `postgres`, `postgres-openssl`, `base64`, `scrypt`, `sha2`, `time` | Hub validation and datastore helpers are opt-in. |
| `falkor` | `redis` | FalkorDB graph commands use the Redis protocol adapter. |
| `qdrant` | `reqwest` with `blocking` and `json` | Vector search/storage helpers need HTTP. Other consumers should not pull reqwest. |
| `indexing` | `ignore`, `sha2` | File walking and content hashing are useful for indexing consumers only. |
| `search` | no extra dependency today | Search fusion contracts are lightweight, but still opt-in so the public surface remains explicit. |
| `graph-analytics` | no extra dependency today | In-memory graph analytics remain opt-in so the public surface stays explicit. |
| `ai` | `reqwest`, `ureq`, and AI payload helpers | AI transport, daemon routing helpers, profile tiers, agentic/tool-loop generation, and the shared blocking OpenAI-compatible embeddings client (`ai::embeddings`, consumed by gcode for grant-backed embedding requests) need HTTP clients and multipart payload support. |
| `full` | all feature modules | Convenience feature for development and consumers that need the whole foundation layer. |

Every individual feature must compile in isolation. Do not rely on `--all-features` to hide missing feature dependencies.

## Versioning Policy

`gobby-core` is `0.x`. The contract:

- **Patch bumps (0.4.x)** — bug fixes, doc changes, internal refactors with no public API change.
- **Additive minor bumps (for example, 0.7.0 → 0.8.0, marked additive)** — new public API such as functions, structs, fields, or feature-gated modules. Existing consumers stay compatible.
- **Pre-1.0 breaking minor bumps (for example, 0.8.0 → 0.9.0, marked breaking)** — removals, renames, type changes, feature default changes, or semantic contract changes. Bump the minor and bump *every* consumer crate's `gobby-core` dependency in the same release. Don't strand consumers on an old `gobby-core`.

Consumers that depend only on additive minor-line compatibility can pin to a minor version (`gobby-core = "0.7"`). In-tree crates released with `gobby-core` must move to the new minor when that minor is breaking, and should pin to the current patch floor when they rely on behavior from that patch, for example `gobby-core = "0.10.0"`.

## How to Consume

### In-tree (workspace crates)

```toml
[dependencies]
gobby-core = { path = "../gcore", version = "0.10.0" }
```

The `path` is for local workspace builds; `version` is required by `cargo publish` and gets used when consumers install the crate from crates.io. Don't drop the `version` field — `cargo publish` will reject the consumer's manifest.

Opt in to heavier modules explicitly:

```toml
[dependencies]
gobby-core = { path = "../gcore", version = "0.10.0", features = ["postgres", "search"] }
```

Small binaries should keep the default empty feature set unless they directly use a feature-gated module.

### Out-of-tree

```toml
[dependencies]
gobby-core = "0.10.0"
```

Resolves against crates.io. The empty default feature set excludes the optional
PostgreSQL, FalkorDB, Qdrant and indexing integrations. It still includes
`ureq`, `fernet` and vendored OpenSSL; inspect the current manifest rather than
assuming every substantial dependency is feature-gated.

### AI Generation and Secrets

The `ai` feature owns both one-shot generation and agentic/tool-loop generation.
Consumers choose the tier and transport; `gobby-core` supplies shared profile
resolution, request/response contracts, retry/timeout behavior, and
provider-neutral loop accounting. `daemon_agentic_chat` sends the daemon a
feature profile, project context, max-turn settings, reasoning effort, and a
`ToolPolicy` that lists allowed tools and whether mutation is allowed.

AI config values resolve from grant-backed and daemon-served sources. Unresolved
secret markers that reach a client are grant-issuance bugs and fail typed.

## Adding a New Helper

Before adding a module or function to `gobby-core`, check:

1. **Do at least two binaries need it?** If only one does, keep it in that binary.
2. **Does it belong in an existing boundary?** Prefer `config`, `ai_context`, `degradation`, `grant`, `schema`, `postgres`, `falkor`, `qdrant`, `indexing`, or `search` before adding a new top-level module.
3. **Is it dependency-light, or properly feature-gated?** New baseline deps propagate to *every* binary. Heavy deps belong behind a narrowly named feature. The existing baseline includes `ureq`, `fernet`, and vendored OpenSSL; an empty feature set does not mean dependency-free.
4. **Does it stay daemon-granted?** Runtime helpers consume grant-backed configuration and do not create, alter, drop, or migrate Gobby-owned schema or datastore objects.
5. **Is it stateless or near-stateless?** `gobby-core` functions are pure or do narrow I/O (read one file, return result). A module that holds connection pools or background workers belongs elsewhere.
6. **Is the public surface small?** A few focused functions and structs per module is the right order of magnitude. If you find yourself adding a builder, a config object, and an `init()` function, reconsider.

If yes to all checks, add the helper:

1. Add it to the appropriate module with `//!` or item docs.
2. For a new lightweight module, add `pub mod <name>;` to `crates/gcore/src/lib.rs`.
3. For a new heavy module, add an optional dependency, a feature entry, and a `#[cfg(feature = "<feature>")] pub mod <name>;` guard.
4. Write tests that pin behavior under the failure modes the consumer cares about (missing input, malformed input, edge-case values).
5. Update this guide's module map and feature gate table when the public boundary changes.
6. Bump `gobby-core` to the next minor version since you're adding public API.
7. Update consumer crates to use the new helper, replacing any duplicated implementation. For breaking minor bumps, update every consumer crate's `gobby-core` dependency in the same release. Bump consumer package versions when those crates are part of the release.

## Testing

Behavioral modules use `#[cfg(test)] mod tests` with `tempfile::tempdir()` for filesystem isolation:

- **project**: direct unit tests cover non-destructive identity reads, fallback
  markers and complete foreign isolation markers; consumers add their own
  stricter checkout-registration tests.
- **bootstrap**: missing/malformed/empty files all return defaults; custom port/host parsing; out-of-range port falls back to default.
- **daemon_url**: wildcard IPv4/IPv6 and localhost normalize to loopback; custom
  host+port composes correctly; bare IPv6 literals get bracketed; URL, port,
  deprecated port alias and explicit bootstrap URL precedence are tested.
- **public_boundary**: integration test that pins feature gates, `lib.rs` module guards, and this guide's boundary documentation.

```bash
cargo nextest run -p gobby-core --no-default-features
cargo test --doc -p gobby-core --no-default-features
```

Baseline tests are fast, perform no network I/O, and keep filesystem writes inside temporary directories.

## Design Decisions

### Why Infallible Defaults Instead of `Result`

`read_daemon_endpoint` and friends return `DaemonEndpoint` (not `Result<DaemonEndpoint>`). The reasoning:

- Every consumer wants *some* endpoint to dial. Erroring at startup because `~/.gobby/bootstrap.yaml` doesn't exist would force every binary to handle the error identically (fall back to loopback + 60887). Centralizing that fallback here is the right move.
- The daemon defaults are well-known and stable. There's no "right" error message to surface — "use loopback" is always the answer.
- If a binary genuinely needs to know whether the file existed (e.g. for a setup-wizard prompt), it can call `bootstrap_path()` and `Path::exists()` directly.

`read_project_id` *does* return `Result` because there's no sane default for "I asked for a project ID and there isn't one" — the caller has to decide what that means.

### Why Listen-Address Normalization Lives in `daemon_url`, Not `bootstrap`

`bootstrap` returns the raw endpoint as written so callers can distinguish "user configured `0.0.0.0` for LAN exposure" from "user configured `127.0.0.1`." `daemon_url` is the layer concerned with *dialing*, so that's where the rewrite happens. Diagnostic tooling that wants to display the actual `bind_host` (e.g. `ghook --diagnose`) reads from `bootstrap` directly.

### Why Not Re-Export from a Prelude

There's no `gobby_core::prelude`. The crate is small enough that explicit imports (`use gobby_core::project::find_project_root`) are clearer than a glob. Keep it that way until the public surface grows past ~10 items.

_Last verified: 2026-09-12_
