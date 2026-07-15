# Rust Helper Release Guide

This guide covers the multi-crate Rust release flow for maintainers. Releases
are cut from the `GobbyAI/gobby` monorepo; the legacy `GobbyAI/gobby-cli` repo
holds pre-monorepo release history only.

## Current Release Set

| Crate | Binary | Version | Tag | Publishes? |
|---|---|---:|---|---|
| `gobby-core` | n/a | `0.8.1` | `gobby-core-v0.8.1` | crates.io only |
| `gobby-code` | `gcode` | `1.5.0` | `gcode-v1.5.0` | crates.io + GitHub binaries |
| `gobby-hooks` | `ghook` | `0.7.2` | `ghook-v0.7.2` | crates.io + GitHub binaries |
| `gobby-wiki` | `gwiki` | `0.8.0` | `gwiki-v0.8.0` | crates.io + GitHub binaries |

## Version Rules

- Bump only crates whose shipped behavior, public API, or release artifacts
  changed.
- `gobby-core` is pre-1.0. A minor bump is breaking under Cargo semver, so every
  active consumer crate's `gobby-core` path dependency must move its explicit
  `version` in the same release (`gcode`, `gwiki` dependency and dev-dependency,
  and `ghook`). crates.io rejects path dependencies without a `version` field,
  so never drop it.
- Keep tag prefixes aligned with the package release contract: `gcode-v*`,
  `ghook-v*`, `gwiki-v*`, and `gobby-core-v*`.

## Merge Order

Release prep lands on the active development branch first (currently `0.5.0`).
After validation passes, push the branch, sync `main`, and merge it into
`main` with:

```text
Merge 0.5.0 into main for release: gobby-core 0.8.1, gcode 1.5.0, ghook 0.7.2, gwiki 0.8.0
```

Push `main` and wait for main CI to pass before tagging. Tags are lightweight
and are pushed by the maintainer from the passing `main` HEAD.

## Tag Order

When `gobby-core` changes, publish the upstream library before binaries that
depend on it. Every binary crate resolves `gobby-core` from crates.io at publish
time, so the new core version must be indexed first. `gwiki` additionally
re-verifies that the published `gobby-core` exposes the `ai` feature.

```bash
git tag gobby-core-v0.8.1
git push origin gobby-core-v0.8.1

# Wait for crates.io to index gobby-core 0.8.1.

git tag gcode-v1.5.0
git tag ghook-v0.7.2
git tag gwiki-v0.8.0

# Push the tags ONE AT A TIME. GitHub Actions does not create push events for
# any tag when more than three tags arrive in a single push, so a batched
# `git push origin <tag> <tag> <tag> <tag> ...` silently triggers NO release
# workflows. Push each tag in its own invocation:
for tag in gcode-v1.5.0 ghook-v0.7.2 gwiki-v0.8.0; do
  git push origin "refs/tags/$tag"
done
```

If a batch push already created the tags on the remote without triggering
workflows, delete them first (`git push origin --delete <tag> ...`) and re-push
individually as above — re-pushing an existing remote tag ref is a no-op and
fires no event.

The release workflows verify binary crate tag/version alignment where the
installer expects GitHub assets. `gobby-core` has no binary artifact matrix.

All four crates publish to crates.io with the repository `CARGO_REGISTRY_TOKEN`
secret via `cargo publish` (which reads the token from the environment). `gwiki`
previously used GitHub OIDC Trusted Publishing, but that path needs a
crates.io-side config that cannot be registered from the CLI, so its publish job
now matches `gcode`/`ghook`/`gobby-core`.

## Local Install Check

Build release binaries locally before tagging and copy the released binaries
into the Gobby bin directory:

```bash
cargo build --release -p gobby-code -p gobby-hooks -p gobby-wiki
mkdir -p ~/.gobby/bin
cp target/release/gcode target/release/ghook target/release/gwiki ~/.gobby/bin/
~/.gobby/bin/gcode --version
~/.gobby/bin/ghook --version
~/.gobby/bin/gwiki --version
```

Refresh each `~/.gobby/bin/.<name>-version` sidecar to the released version, and
run `ghook --version` so it rewrites `~/.gobby/bin/.ghook-runtime.json`.

## Validation

Before tagging, run the focused release checks:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo clippy -p gobby-core --all-targets -- -D warnings
cargo clippy -p gobby-core --all-targets --no-default-features -- -D warnings
cargo clippy -p gobby-code -- -D warnings
cargo clippy -p gobby-code --no-default-features -- -D warnings
cargo clippy -p gobby-hooks --all-targets -- -D warnings
cargo clippy -p gobby-hooks --all-targets --no-default-features -- -D warnings
cargo clippy -p gobby-wiki --all-targets -- -D warnings
cargo clippy -p gobby-wiki --all-targets --no-default-features -- -D warnings
cargo nextest run --workspace --no-default-features
cargo test --doc --workspace --no-default-features
cargo nextest run -p gobby-core
cargo test --doc -p gobby-core
cargo nextest run -p gobby-code
cargo test --doc -p gobby-code
cargo nextest run -p gobby-hooks
cargo test --doc -p gobby-hooks
cargo nextest run -p gobby-wiki
cargo test --doc -p gobby-wiki
cargo build --workspace --no-default-features
cargo build --release -p gobby-code -p gobby-hooks -p gobby-wiki
```

The repository CI still owns cross-target release packaging. Local validation
only proves manifests, lockfile resolution, and native release binaries.

_Last verified: 2026-07-14_
