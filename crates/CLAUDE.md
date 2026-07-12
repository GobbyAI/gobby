# Rust Workspace (`crates/`)

The former `gobby-cli` repo now lives here — Gobby is a monorepo. The Rust code is
a Cargo workspace (`Cargo.toml`, `Cargo.lock`, and `rust-toolchain.toml` at the
repo root). Use `cargo` for all Rust operations, respect `rust-toolchain.toml`,
and load the `rust` skill before editing Rust. Detailed Rust conventions live in
`AGENTS.md`.

```bash
# Build / check a specific crate (use -p; avoid whole-workspace builds unless needed)
cargo build -p gobby-code                 # gcode CLI
cargo build --release -p gobby-code       # release artifact (installed to ~/.gobby/bin/gcode)

# Lint & format — match repo config; never relax lints to pass
cargo clippy -p <package>                 # e.g. gobby-code, gobby-core, gobby-hooks, gobby-wiki
cargo fmt -p <package> -- --check         # drop --check to auto-format

# Tests — scope with -p (or a test name). NEVER run bare `cargo test` across the workspace
cargo test -p gobby-code
cargo test <name> -p gobby-code
```

Crate → binary map: `gobby-code` → `gcode`, `gobby-hooks` → `ghook`,
`gobby-wiki` → `gwiki`; `gobby-core` is the shared library crate. The daemon and
hooks shell out to the installed `~/.gobby/bin/{gcode,ghook,gwiki}` binaries, so
rebuild **and reinstall** those after changing crate behavior — a committed change
is not live until the binary is reinstalled.
