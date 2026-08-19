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
cargo build --release -p gobby-code       # release artifact (installed to ~/.gobby/bin/gcode)
cargo build --release -p gobby-daemon     # schema CLI (installed to ~/.gobby/bin/gdaemon)

# Lint & format — match repo config; never relax lints to pass
cargo clippy -p <package>                 # e.g. gobby-code, gobby-core, gobby-daemon, gobby-hooks, gobby-wiki
cargo fmt -p <package> -- --check         # drop --check to auto-format

# Tests — scope with -p (or a test name); bare `cargo test` builds and runs the
# whole workspace, which is slow and mostly irrelevant to your change
cargo test -p gobby-code
cargo test <name> -p gobby-code
```

Inline `#[cfg(test)]` modules count toward the owning production file's
1,000-line ceiling. Keep large unit-test modules out of production Rust files.
Place the tests at `<module>/tests.rs` and declare them from `<module>.rs` with:

```rust
#[cfg(test)]
#[path = "<module>/tests.rs"]
mod tests;
```

The crate → binary map and the rebuild-and-reinstall requirement (including the
new-inode install step macOS needs) live in `AGENTS.md` under Architecture Facts.
