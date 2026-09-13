# Rust Helper Release Guide

This guide covers the multi-crate Rust release flow for maintainers. Releases
are cut from the `GobbyAI/gobby` monorepo; the legacy `GobbyAI/gobby-cli` repo
holds pre-monorepo release history only.

## Current Release Set

| Crate | Binary | Version | Tag | Publishes? |
|---|---|---:|---|---|
| `gobby-core` | n/a | `0.10.0` | `gobby-core-v0.10.0` | crates.io only |
| `gobby-code` | `gcode` | `1.7.0` | `gcode-v1.7.0` | crates.io + GitHub binaries |
| `gobby-daemon` | `gdaemon` | `0.4.0` | `gdaemon-v0.4.0` | crates.io + GitHub binaries |
| `gobby-hooks` | `ghook` | `0.9.0` | `ghook-v0.9.0` | crates.io + GitHub binaries |
| `gobby-terminal` | `gterm` | `0.1.0` | `gterm-v0.1.0` | crates.io + GitHub binaries (four Stage-0 triples) |
| `gobby-client` | `gclient` | `0.1.0` | `gclient-v0.1.0` | crates.io + GitHub binaries (four Stage-0 triples) |

## Version Rules

- Bump only crates whose shipped behavior, public API, or release artifacts
  changed.
- `gobby-core` is pre-1.0. A minor bump is breaking under Cargo semver, so every
  active consumer crate's `gobby-core` path dependency must move its explicit
  `version` in the same release. crates.io rejects path dependencies without
  a `version` field, so never drop it.
- Keep tag prefixes aligned with the package release contract: `gcode-v*`,
  `gdaemon-v*`, `ghook-v*`, `gterm-v*`, `gclient-v*`, and
  `gobby-core-v*`.
- Publish `gobby-terminal` version *V* before tagging `gclient-v*` that
  depends on *V*. `gobby-client` cannot publish until that exact
  `gobby-terminal` version is on crates.io.

## Merge Order

Release prep lands on the active development branch first (currently `0.5.0`).
After validation passes, push the branch, sync `main`, and merge it into
`main` with:

```text
Merge 0.5.0 into main for release: gobby-core 0.10.0, gcode 1.7.0, gdaemon 0.4.0, ghook 0.9.0
```

Push `main` and wait for main CI to pass before tagging. Tags are lightweight
and are pushed by the maintainer from the passing `main` HEAD.

## Tag Order

When `gobby-core` changes, publish the upstream library before binaries that
depend on it. Every binary crate resolves `gobby-core` from crates.io at publish
time, so the new core version must be indexed first.

```bash
git tag gobby-core-v0.10.0
git push origin gobby-core-v0.10.0

# Wait for crates.io to index gobby-core 0.10.0.

git tag gcode-v1.7.0
git tag gdaemon-v0.4.0
git tag ghook-v0.9.0

# Push the tags ONE AT A TIME. GitHub Actions does not create push events for
# any tag when more than three tags arrive in a single push, so a batched
# `git push origin <tag> <tag> <tag> <tag> ...` silently triggers NO release
# workflows. Push each tag in its own invocation:
for tag in gcode-v1.7.0 gdaemon-v0.4.0 ghook-v0.9.0; do
  git push origin "refs/tags/$tag"
done
```

These versioned commands illustrate the manifest versions in the table; they
are not an instruction to recreate existing release tags. Before publishing,
verify each target tag is absent and select the intended release commit.
If remote tags already exist without a workflow run, inspect their commits,
release state, and published artifacts before choosing a maintainer recovery.
Do not delete or move published tags as routine retry behavior.

GitHub documents the three-tag event limit in its
[push event reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#push).

The release workflows verify binary crate tag/version alignment where the
installer expects GitHub assets. `gobby-core` has no binary artifact matrix.

Published crates use the repository `CARGO_REGISTRY_TOKEN` secret via
`cargo publish`, which reads the token from the environment.

## Local Install Check

From the main checkout, use the shared cutover command to build and promote
`gcode`, `gdaemon`, and `ghook` as one coherent set:

```bash
uv run gobby cutover --path .
~/.gobby/bin/gcode --version
~/.gobby/bin/gdaemon --version
~/.gobby/bin/ghook --version
```

Cutover signs staged binaries when required, promotes through new inodes,
writes version/install sidecars and the common schema identity pin, verifies
the resolved daemon binary, then restarts the daemon. Coordinate the restart
with active sessions. Do not overwrite installed signed binaries in place or
write identity pins by hand. See the
[cutover reference](cli-commands.md#gobby-cutover).

## Validation

Before tagging, run the focused release checks:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo clippy -p gobby-core --all-targets -- -D warnings
cargo clippy -p gobby-core --all-targets --no-default-features -- -D warnings
cargo clippy -p gobby-code -- -D warnings
cargo clippy -p gobby-code --no-default-features -- -D warnings
cargo clippy -p gobby-daemon --all-targets -- -D warnings
cargo clippy -p gobby-daemon --all-targets --no-default-features -- -D warnings
cargo clippy -p gobby-hooks --all-targets -- -D warnings
cargo clippy -p gobby-hooks --all-targets --no-default-features -- -D warnings
cargo nextest run --workspace --no-default-features
cargo test --doc --workspace --no-default-features
cargo nextest run -p gobby-core
cargo test --doc -p gobby-core
cargo nextest run -p gobby-code
cargo test --doc -p gobby-code
cargo nextest run -p gobby-daemon
cargo nextest run -p gobby-hooks
cargo test --doc -p gobby-hooks
cargo build --workspace --no-default-features
cargo build --release -p gobby-code -p gobby-daemon -p gobby-hooks
```

The repository CI still owns cross-target release packaging. Local validation
only proves manifests, lockfile resolution, and native release binaries.

_Last verified: 2026-09-13_
