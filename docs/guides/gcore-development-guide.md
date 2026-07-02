# gobby-core Development Guide

Technical internals for developers and agents working in the `gobby-core` crate (`crates/gcore/`).

## What gobby-core Is

`gobby-core` is the shared Rust foundation crate for Gobby CLI crates and future Rust daemon work. It holds the boring, reusable platform layer: project discovery, bootstrap and daemon addressing, shared context/config contracts, setup boundaries, degradation vocabulary, feature-gated datastore adapters, and generic indexing/search primitives.

Domain behavior stays out of this crate. Code graph facts, symbol IDs, language parsing policy, wiki vault layout, task behavior, memory behavior, and CLI output formatting belong to consumer crates.

The baseline crate remains dependency-light. Consumers that only need project discovery and daemon helpers do not inherit PostgreSQL, FalkorDB, Qdrant, reqwest, ignore, or sha2 unless they opt in through Cargo features.

## Module Map

`crates/gcore/src/`:

| Module | Feature | Responsibility |
|--------|---------|----------------|
| `project` | always | Walk up from a starting directory to find a `.gobby/` directory containing `project.json` or `gcode.json`. Read the `id` field from the project identity file. |
| `bootstrap` | always | Read `~/.gobby/bootstrap.yaml` to get the daemon's listen endpoint (`bind_host`, `daemon_port`). Falls back to `127.0.0.1:60887` when the file is missing or malformed. |
| `daemon_url` | always | One daemon-URL resolver for all binaries: `GOBBY_DAEMON_URL` → `GOBBY_PORT` → bootstrap endpoint, normalizing wildcard listen addresses (`0.0.0.0`, `::`, `::0`) to `127.0.0.1` and bracketing bare IPv6 literals. |
| `codewiki_contract` | always | Frontmatter contract for codewiki-generated vault pages: shared key/value constants plus a golden page fixture pinned by gcode's emitter tests and gwiki's parser tests. |
| `config` | always | Shared configuration-resolution contracts. Environment variables, `config_store`, and defaults are represented here as the foundation expands. |
| `context` | always | Shared runtime context contracts for project identity, daemon URL, and service configuration. Consumer-specific CLI state stays outside. |
| `degradation` | always | Shared vocabulary for configured-service unavailability, explicit degraded paths, partial search, stale indexes, skipped artifacts, and fatal core errors. |
| `ai` | `ai` | Shared AI routing, direct/daemon transports, profile tiers, embeddings, and agentic/tool-loop generation primitives. |
| `setup` | always | Attached and standalone setup contracts. Runtime commands validate externally managed resources and do not implicitly migrate them. |
| `token_budget` | always | Shared token-budget trimming helpers — bounds prompt/context payloads to a token ceiling so consumers (gwiki `ask`, gcode codewiki) reuse one budgeting primitive. |
| `postgres` | `postgres` | PostgreSQL hub adapter boundary. Validates Gobby-owned schema and BM25 requirements without creating, altering, or dropping managed objects. |
| `falkor` | `falkor` | FalkorDB adapter boundary. Graph connection helpers live here without making FalkorDB a baseline dependency. |
| `qdrant` | `qdrant` | Qdrant adapter boundary for vector search/storage integration. |
| `indexing` | `indexing` | Generic file walking, hashing, and indexing primitives that are not tied to one domain model. |
| `search` | `search` | Generic search result and fusion primitives. Code-specific or wiki-specific search behavior stays in consumers. |
| `graph_analytics` | `graph-analytics` | In-memory graph analytics — weighted Leiden community detection over a consumer-supplied graph. No optional dependency; gated so the public surface stays explicit. |

Feature-gated modules are part of the public module map but compile only when their feature is selected.

## Public API

### `project`

```rust
pub fn find_project_root(start: &Path) -> Option<PathBuf>;
pub fn read_project_id(project_root: &Path) -> anyhow::Result<String>;
```

`find_project_root` walks up from `start` looking for a `.gobby/project.json` (Gobby-managed) or `.gobby/gcode.json` (gcode-standalone). Returns the directory *containing* `.gobby/`, not `.gobby/` itself. Returns `None` when neither marker is found before hitting the filesystem root.

`read_project_id` reads `<root>/.gobby/project.json` and extracts the `id` field. When `project.json` is absent, it reads the gcode-owned `<root>/.gobby/gcode.json` identity file so standalone gcode roots found by `find_project_root` remain usable. Errors if neither file can be read, the JSON is malformed, or the field isn't present.

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

pub struct DaemonEndpoint { pub host: String, pub port: u16 }

pub fn bootstrap_path() -> Option<PathBuf>;
pub fn read_daemon_endpoint() -> DaemonEndpoint;
pub fn read_daemon_endpoint_at(path: &Path) -> DaemonEndpoint;
```

`read_daemon_endpoint` is the lookup callers want. `read_daemon_endpoint_at` exists for tests and for callers who already know the path. Both return `DaemonEndpoint::default()` (loopback + 60887) on any failure — missing file, unreadable, malformed YAML, missing fields, no home directory. **No errors are surfaced**; clients should always get *something* usable.

`DaemonEndpoint` returns the raw endpoint as written. `0.0.0.0` and `::` are valid listen addresses but invalid dial addresses — normalization is the caller's job, or the `daemon_url` module's, not this one's.

### `daemon_url`

```rust
pub fn daemon_url() -> String;
pub fn daemon_url_at(path: &Path) -> String;
```

The one daemon-URL resolver every Gobby binary shares. `daemon_url()` applies a single env-override contract before reading bootstrap:

1. `GOBBY_DAEMON_URL` — full base-URL override; trailing slashes are trimmed, empty values ignored.
2. `GOBBY_PORT` — port-only override, dialed as `http://127.0.0.1:{port}`; empty or unparseable values ignored.
3. `bootstrap.yaml` endpoint, with dial normalization.

`daemon_url_at(path)` reads a specific bootstrap file and never consults the env — an explicit path is already an override.

Dial normalization rewrites wildcard listen hosts (`0.0.0.0`, `::`, `::0`, `[::]`) and empty hosts to `127.0.0.1`, and brackets bare IPv6 literals (`::1` → `[::1]`) for URL embedding. Hostnames, named interfaces, and explicit IPv4 literals pass through unchanged.

```rust
let url = gobby_core::daemon_url::daemon_url();
// "http://127.0.0.1:60887" for default bootstrap
// "http://10.0.0.5:61234" if bootstrap has bind_host: 10.0.0.5
// "http://127.0.0.1:60887" if bootstrap has bind_host: 0.0.0.0
// GOBBY_DAEMON_URL / GOBBY_PORT win over all of the above
ureq::post(&format!("{url}/api/hooks/execute")).send_string(body)?;
```

### `falkor`

```rust
pub struct GraphClient {
    graph: SyncGraph,
}

impl GraphClient {
    pub fn from_config(config: &FalkorConfig, graph_name: &str) -> anyhow::Result<Self>;
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>>;
    pub fn with_sync_graph<T>(
        &mut self,
        f: impl FnOnce(&mut ReadOnlySyncGraph<'_>) -> anyhow::Result<T>,
    ) -> anyhow::Result<T>;
}
```

Consumers provide the graph name through constructor methods such as `GraphClient::from_config`; `gobby-core` must not hardcode code, wiki, or memory graph defaults. The `graph` field stays private so connection ownership cannot leak across domain crates. Use `query` for normal Cypher reads/writes. `with_sync_graph` is the narrow escape hatch for consumers that need a FalkorDB crate operation not yet represented by the shared adapter.
The closure receives `&mut ReadOnlySyncGraph<'_>` because the FalkorDB crate
requires mutable access even for `GRAPH.RO_QUERY`; the wrapper exposes only the
read-only query builder and selected graph name.

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

`gobby-wiki` should use the same contracts for wiki search and indexing. Missing vector search, stale vault index data, or skipped files should be reported as degradation metadata alongside partial results. A required store or write path failure should become `CoreError` only when the command cannot complete.

`Guidance` and `SetupIssue` carry structured setup remediation for attached and standalone validation. Consumer CLIs render the `problem`, `action`, and optional `command_hint` fields in their own output style; `gobby-core` only provides the serializable contract.

### `setup`

`setup` defines the shared contracts for two separate workflows:

- **Attached mode** uses `AttachedValidator` and `RequiredObject` declarations to check that externally managed resources already exist. It returns a `ValidationReport` containing present objects and missing objects with typed `SetupIssue` guidance. `gobby-core` does not create, alter, drop, or migrate Gobby-owned schema in attached mode.
- **Standalone mode** uses `StandaloneSetup` and `OwnedObject` declarations for explicit setup commands that create consumer-owned resources. Consumers must declare a namespace such as `gcode` or `gwiki` so owned tables, graph labels, and vector collections stay domain-scoped.

`ValidationContext` and `SetupContext` pass nullable datastore handles/configuration into callbacks so diagnostics and explicitly degraded paths can represent absence. PostgreSQL handles are mutable because `postgres::Client::query` and `postgres::Client::execute` both require `&mut self`; the callbacks borrow the supplied context and do not take ownership from later validators or creators.

## Boundary Rules

Each module exists because multiple Rust consumers need the same infrastructure contract, and getting it slightly wrong in one crate would silently misbehave.

| Boundary | Consumers | What stays out |
|----------|-----------|----------------|
| Project/bootstrap/daemon helpers | `gcode`, `ghook`, future Rust consumers | CLI rendering, command dispatch, daemon workflow semantics. |
| Context/config/setup/degradation contracts | `gcode`, `gobby-wiki`, future daemon work | Domain-specific flags, output formats, setup UX, and task/memory behavior. |
| Datastore adapters | Consumers that opt in to `postgres`, `falkor`, or `qdrant` | Schema ownership, migrations, code graph facts, vector content policy. |
| Indexing/search primitives | Consumers that opt in to `indexing` or `search` | Code symbol IDs, language parsing policy, wiki document models, ranking UX. |

`gobby-core` can validate attached-mode resources, but it must not create, alter, drop, or migrate Gobby-owned resources during normal runtime commands.

## Feature Gates

The crate's default feature set is empty:

```toml
[features]
default = []
postgres = ["dep:postgres", "dep:postgres-types", "dep:postgres-openssl", "dep:base64", "dep:scrypt"]
falkor = ["dep:falkordb", "dep:urlencoding"]
qdrant = ["dep:reqwest", "dep:urlencoding"]
indexing = ["dep:ignore", "dep:sha2"]
search = []
graph-analytics = []
ai = ["dep:reqwest", "dep:base64", "dep:bytes", "dep:httpdate", "dep:rand", "dep:ureq", "reqwest/multipart"]
full = ["postgres", "falkor", "qdrant", "indexing", "search", "graph-analytics", "ai"]
```

`openssl` is intentionally **not** feature-gated: the always-compiled
`fernet`/`secrets` path pulls `openssl-sys` into every dependency graph, so the
crate declares `openssl = { version = "0.10", features = ["vendored"] }` as a
non-optional dependency. This unifies `openssl-sys` to a static (vendored) build
across all consumers and targets — including graphs that omit `postgres`
(ghook, gcode's build-dependency graph) — which the Windows release runners
require because they have no system OpenSSL.

Feature rationale:

| Feature | Enables | Why gated |
|---------|---------|-----------|
| `postgres` | `postgres`, `postgres-types`, `postgres-openssl`, `base64`, `scrypt` | Hub validation, adapter code, and datastore-backed secret/config helpers are only needed by datastore consumers. Lightweight binaries should not inherit PostgreSQL. |
| `falkor` | `falkordb`, `urlencoding` | Graph helpers need FalkorDB. `urlencoding` is included because FalkorDB connection URLs must encode passwords safely. |
| `qdrant` | `reqwest` with `blocking` and `json` | Vector search/storage helpers need HTTP. Other consumers should not pull reqwest. |
| `indexing` | `ignore`, `sha2` | File walking and content hashing are useful for indexing consumers only. |
| `search` | no extra dependency today | Search fusion contracts are lightweight, but still opt-in so the public surface remains explicit. |
| `graph-analytics` | no extra dependency today | In-memory graph analytics remain opt-in so the public surface stays explicit. |
| `ai` | `reqwest`, `ureq`, and AI payload helpers | AI transport, daemon probing, routing helpers, direct/daemon chat transports, profile tiers, agentic/tool-loop generation, and the shared blocking OpenAI-compatible embeddings client (`ai::embeddings`, consumed by gcode and gwiki for direct embedding requests) need HTTP clients and multipart payload support. |
| `full` | all feature modules | Convenience feature for development and consumers that need the whole foundation layer. |

Every individual feature must compile in isolation. Do not rely on `--all-features` to hide missing feature dependencies.

## Versioning Policy

`gobby-core` is `0.x`. The contract:

- **Patch bumps (0.4.x)** — bug fixes, doc changes, internal refactors with no public API change.
- **Minor bumps (0.x.0)** — additive public API (new functions, new fields). Existing consumers stay compatible.
- **Pre-1.0 breaking changes** — bump the minor and bump *every* consumer crate's gobby-core dep in the same release. Don't strand consumers on an old gobby-core.

Consumers that depend only on the minor-line contract can pin to a minor version (`gobby-core = "0.7"`). In-tree crates released with `gobby-core` should pin to the current patch floor when they rely on behavior from that patch, for example `gobby-core = "0.7.0"`.

## How to Consume

### In-tree (workspace crates)

```toml
[dependencies]
gobby-core = { path = "../gcore", version = "0.7.0" }
```

The `path` is for local workspace builds; `version` is required by `cargo publish` and gets used when consumers install the crate from crates.io. Don't drop the `version` field — `cargo publish` will reject the consumer's manifest.

Opt in to heavier modules explicitly:

```toml
[dependencies]
gobby-core = { path = "../gcore", version = "0.7.0", features = ["postgres", "search"] }
```

Small binaries should keep the default empty feature set unless they directly use a feature-gated module.

### Out-of-tree

```toml
[dependencies]
gobby-core = "0.7.0"
```

Resolves against crates.io. The default crate has no datastore dependencies. It will not pull in PostgreSQL, FalkorDB, Qdrant, reqwest, ignore, sha2, tokio, tracing, or anything else heavy unless the consumer selects the matching feature.

### AI Generation and Secrets

The `ai` feature owns both one-shot generation and agentic/tool-loop generation.
Consumers choose the tier and transport; `gobby-core` supplies shared profile
resolution, request/response contracts, retry/timeout behavior, and
provider-neutral loop accounting. `daemon_agentic_chat` sends the daemon a
feature profile, project context, max-turn settings, reasoning effort, and a
`ToolPolicy` that lists allowed tools and whether mutation is allowed. CodeWiki
uses a read-only policy for aggregate page investigation.

AI config values resolve from datastore-backed config sources first, then
standalone `~/.gobby/gcore.yaml`, then defaults. Datastore-backed sources may
resolve `$secret:NAME` through the Gobby secret store; env-only sources reject
secret references so unresolved placeholders do not leak into outbound HTTP
headers or daemon requests. Keep Fernet/DEK handling in `secrets`; config
resolvers should call the source's value resolver rather than decrypting
directly.

## Adding a New Helper

Before adding a module or function to `gobby-core`, check:

1. **Do at least two binaries need it?** If only one does, keep it in that binary.
2. **Does it belong in an existing boundary?** Prefer `config`, `context`, `degradation`, `setup`, `postgres`, `falkor`, `qdrant`, `indexing`, or `search` before adding a new top-level module.
3. **Is it dependency-light, or properly feature-gated?** New baseline deps propagate to *every* binary. Heavy deps belong behind a narrowly named feature.
4. **Does it respect setup mode?** Attached-mode helpers validate externally managed state. Standalone setup helpers run only through explicit setup flows.
5. **Is it stateless or near-stateless?** `gobby-core` functions are pure or do narrow I/O (read one file, return result). A module that holds connection pools or background workers belongs elsewhere.
6. **Is the public surface small?** A few focused functions and structs per module is the right order of magnitude. If you find yourself adding a builder, a config object, and an `init()` function, reconsider.

If yes to all checks, add the helper:

1. Add it to the appropriate module with `//!` or item docs.
2. For a new lightweight module, add `pub mod <name>;` to `crates/gcore/src/lib.rs`.
3. For a new heavy module, add an optional dependency, a feature entry, and a `#[cfg(feature = "<feature>")] pub mod <name>;` guard.
4. Write tests that pin behavior under the failure modes the consumer cares about (missing input, malformed input, edge-case values).
5. Update this guide's module map and feature gate table when the public boundary changes.
6. Bump `gobby-core` to the next minor version since you're adding public API.
7. Update consumer crates to use the new helper, replacing any duplicated implementation. Bump consumer package versions when those crates are part of the release.

## Testing

Behavioral modules use `#[cfg(test)] mod tests` with `tempfile::tempdir()` for filesystem isolation:

- **project**: implicitly tested via consumer binaries (`gcode`, `ghook`); the module mirrors `gcode/src/project.rs` line-for-line.
- **bootstrap**: missing/malformed/empty files all return defaults; custom port/host parsing; out-of-range port falls back to default.
- **daemon_url**: wildcard IPv4/IPv6 normalize to loopback; localhost passes through; custom host+port composes correctly; bare IPv6 literals get bracketed; `GOBBY_DAEMON_URL`/`GOBBY_PORT` override precedence and garbage-value handling.
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
