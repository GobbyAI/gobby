# Rust Workspace (`crates/`)

The former `gobby-cli` repo now lives here — Gobby is a monorepo. The Rust code is
a Cargo workspace (`Cargo.toml`, `Cargo.lock`, and `rust-toolchain.toml` at the
repo root). Use `cargo` for all Rust operations, respect `rust-toolchain.toml`,
and load the `rust` skill before editing Rust. Rust-specific conventions are
defined in this file. Shared architecture facts (crate-to-binary mappings and
the rebuild/reinstall new-inode requirement) live in `AGENTS.md`.

```bash
# Build / check a specific crate (use -p; avoid whole-workspace builds unless needed)
cargo build -p gobby-code                 # gcode CLI
cargo build --release -p <crate>          # one release artifact; install it via a new inode (AGENTS.md)
uv run gobby cutover                      # build/install the release binary set, pin, restart, smoke

# Lint & format — match repo config; never relax lints to pass
cargo clippy -p <package>                 # e.g. gobby-code, gobby-core, gobby-daemon, gobby-hooks
cargo fmt -p <package> -- --check         # drop --check to auto-format

# Tests — run through nextest, the repo's configured runner
# (.config/nextest.toml routes DB-backed tests into a serial group; bare
# `cargo test` bypasses that and flakes). Scope with -p or a filter —
# workspace-wide runs are slow and mostly irrelevant to your change.
cargo nextest run -p gobby-code
cargo nextest run -p gobby-core -E 'test(grant)'
cargo test --doc -p <package>             # nextest does not run doctests

# PostgreSQL-backed gcore schema tests need the postgres feature and a DB:
GOBBY_SCHEMA_TEST_DATABASE_URL=<test-dsn> cargo nextest run -p gobby-core --features postgres

# gcode serial-DB tests compile only when a `*_test` DSN is set at build time;
# the fixture applies the schema and seeds this machine's row itself, so point
# it at an empty database with pg_search (e.g. `gobby_gcode_test` on the
# isolated test hub), never at gobby_test's pytest-managed schema:
GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test \
  cargo nextest run -p gobby-code -E 'test(serial_db)'
```

`gobby-terminal` builds a vendored Zig library (libghostty-vt) whenever the
`vt-engine` feature is on, which the `gterm` binary requires. That build needs
Zig 0.16 on PATH and compiles against whatever macOS SDK Command Line Tools
ships, including 27; no `DEVELOPER_DIR` or Xcode selection is involved.

```bash
cargo build --release -p gobby-terminal --features vt-engine --bin gterm
```

Builds land in one shared directory per project,
`~/.gobby/cache/cargo-target/<project_id>/`: Gobby links `<checkout>/target` there
for every registered checkout and worktree, and sets `CARGO_TARGET_DIR` for spawned
agents. Cargo's build-directory lock serializes concurrent builds across worktrees
("Blocking waiting for file lock on build directory"). A pre-existing real `target/`
directory is left alone; move it aside to join the share. On macOS also raise
the vnode ceiling once per machine, or the daemon's Git commands time out
while cargo runs: see `docs/guides/system-requirements.md`, Troubleshooting.

Because the directory is shared, `target/debug/<bin>` is whatever checkout built
it last, and every worktree sees that same file. Cargo rebuilds when it notices
the sources changed, so building is safe; reading is not. Anything that execs a
binary by path — a test, a script, a probe — must build the crate it needs first
in the checkout it means to test, or it silently runs another branch's build.
Never assume a binary present under `target/` came from the branch you are on.

Inline `#[cfg(test)]` modules count toward the owning production file's
1,000-line ceiling. Keep large unit-test modules out of production Rust files.
Place the tests at `<module>/tests.rs` and declare them from `<module>.rs` with:

```rust
#[cfg(test)]
#[path = "<module>/tests.rs"]
mod tests;
```

## Daemon family crates

Stage 2 of `ROADMAP.md` absorbs the Python daemon one route family at a time,
each as its own crate statically linked into `gdaemon` (decision 16):

- Directory `crates/g<family>`, package `gobby-<family>` (`crates/gtasks` →
  `gobby-tasks`), `publish = false`, version inherited from the workspace.
- Add the crate to the root `Cargo.toml` `members` list when its Stage 2 epic
  starts; no placeholder crates.
- Export one static `RouteFamily`: the prefixes the family claims, its
  `axum::Router` over the shared daemon state, and its service trait. `gdaemon`
  composes families in its routing table; the per-family `Proxy | Native |
  Compare` backend comes from bootstrap.
- Own the family's row types and repositories, built on the `gcore` async pool
  and transaction seam. Reach another family only through its public API;
  Cargo rejects the cycle otherwise, and that is intended.

The crate → binary map and the rebuild-and-reinstall requirement (including the
new-inode install step macOS needs) live in `AGENTS.md` under Architecture Facts.
