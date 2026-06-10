# Configuration - Reference

## Workspace and Package Boundaries

Keep workspace-level policy in the workspace manifest and package-specific policy in each crate:

```toml
[workspace]
members = ["crates/core", "crates/cli"]
resolver = "2"

[workspace.package]
edition = "2021"
rust-version = "1.78"

[workspace.dependencies]
thiserror = "1"
tokio = { version = "1", features = ["rt-multi-thread", "macros"] }
```

Package manifests should inherit shared values when the workspace already centralizes them:

```toml
[package]
name = "profile-client"
edition.workspace = true
rust-version.workspace = true

[dependencies]
thiserror.workspace = true
tokio.workspace = true
```

Do not move a dependency, feature, edition, or resolver setting between package and workspace scope unless every affected crate has been checked.

## Feature Flags

Rust features should be additive. Enabling one feature should not silently disable behavior from another feature.

```toml
[features]
default = ["tls-rustls"]
tls-rustls = ["dep:rustls"]
json = ["dep:serde", "dep:serde_json"]
cli = ["dep:clap", "json"]
```

Use `dep:` entries for optional dependencies so the public feature surface is intentional. Avoid broad features such as `full` unless the crate already exposes that convention.

Validate the feature set you changed:

```bash
cargo check -p profile-client --no-default-features
cargo check -p profile-client --all-features
```

## Toolchain and MSRV

Honor `rust-toolchain.toml` and `rust-version` before using newer language or standard-library APIs:

```toml
[toolchain]
channel = "1.78.0"
components = ["rustfmt", "clippy"]
targets = ["x86_64-unknown-linux-gnu"]
```

Do not bump MSRV as a side effect of an implementation change. If an MSRV bump is required, make it explicit in the task, update compatibility documentation, and validate with the pinned toolchain.

## Formatting and Lints

`rustfmt.toml` and `clippy.toml` are repository policy, not local preferences.

```toml
# clippy.toml
avoid-breaking-exported-api = false
too-many-arguments-threshold = 5
```

Do not loosen lint thresholds, add broad `#[allow(...)]`, or disable formatting to land a change. A narrow suppression needs a comment that states the invariant or external boundary that makes the suppression acceptable.

## Cargo Config

`.cargo/config.toml` can affect compilation through target-specific flags, linkers, aliases, and environment variables:

```toml
[target.x86_64-unknown-linux-musl]
linker = "musl-gcc"

[alias]
xtask = "run -p xtask --"
```

Review it before changing target triples, native dependencies, cross-compilation behavior, or CI commands. Keep machine-local paths out of shared config.

## Focused Validation

Prefer package-scoped commands for agent validation:

```bash
cargo fmt --check
cargo clippy -p profile-client --all-targets --all-features -- -D warnings
cargo nextest run -p profile-client
cargo test -p profile-client --doc
```

Use workspace-wide validation only when the change intentionally crosses workspace policy or shared dependency boundaries.
