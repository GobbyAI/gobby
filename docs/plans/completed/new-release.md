# Release prep: gobby-core 0.7.0, gcode 1.4.0, ghook 0.7.0, gwiki 0.7.0

## Context

This plan tracks the current active Rust release set documented in
`docs/guides/release-guide.md` (`_Last verified: 2026-07-01_`). The release
guide is the source of truth for versions, tag names, tag order, and validation.

Current Cargo.toml versions match the active release set. The one cross-cutting
hazard remains the `gobby-core` dependency floor: every active consumer crate
must keep its explicit path dependency pin at `version = "0.7.0"` or
builds/publishes break.

Decisions (confirmed with user):
- **PR dev→main**, let `ci.yml` + CodeRabbit run, merge once CI is green
  (CodeRabbit treated as advisory).
- **Push tags autonomously** once main CI passes and local validation is clean,
  in dependency order — no pause.

## Target versions

| Crate dir | Package | Binary | Current → New | Tag |
|---|---|---|---|---|
| crates/gcore | `gobby-core` | (lib) | **`0.7.0`** | `gobby-core-v0.7.0` |
| crates/gcode | `gobby-code` | gcode | **`1.4.0`** | `gcode-v1.4.0` |
| crates/ghook | `gobby-hooks` | ghook | **`0.7.0`** | `ghook-v0.7.0` |
| crates/gwiki | `gobby-wiki` | gwiki | **`0.7.0`** | `gwiki-v0.7.0` |

Tag prefix convention (per project memory): short binary name for single-word
packages (`gcode-v*`), full package name for multi-word
(`gobby-core-v*`, `ghook-v*` → publishes `gobby-hooks`, `gwiki-v*` → `gobby-wiki`).

## Step 1 — Version confirmation + gobby-core pin cascade

Confirm the `version =` field in each active crate's `Cargo.toml`:
`crates/gcore = 0.7.0`, `crates/gcode = 1.4.0`, `crates/ghook = 0.7.0`, and
`crates/gwiki = 0.7.0`.

**Critical cascade** — keep the `gobby-core` dependency pin at
`version = "0.7.0"` in every dependent (keep
`path`/`features`/`default-features` exactly as-is):
- `crates/gcode/Cargo.toml` (~line 25)
- `crates/gwiki/Cargo.toml` — **both** the dependency (~line 29) **and** the
  dev-dependency (~line 62)
- `crates/ghook/Cargo.toml` (~line 25)

If any manifest changes are needed, regenerate the lockfile so the versions
land in `Cargo.lock`:
`cargo build --workspace --no-default-features` (commit the updated `Cargo.lock`).

## Step 2 — CHANGELOG.md

Format is Keep-a-Changelog with per-crate `#### <crate>` blocks under
`### Added/Changed/Fixed`, newest release set grouped just under `## [Unreleased]`.

- Fold any existing `[Unreleased]` bullets into the new `[1.4.0] — gcode`,
  `[0.7.0] — gobby-core`, `[0.7.0] — gobby-hooks`, or `[0.7.0] — gwiki` sections; leave
  `## [Unreleased]` as an empty heading.
- Insert four new version headings directly under `[Unreleased]`, above the
  current top entries:
  `## [1.4.0] — gcode`, `## [0.7.0] — gobby-core`, `## [0.7.0] — gobby-hooks`,
  `## [0.7.0] — gwiki`.
- Derive per-crate attribution from the previous release tags to `HEAD` (bucket
  each commit by the crate dirs it touches). Summarize in the house style —
  do **not** transcribe every commit.
  - **gcode (1.4.0):** current stable code-index release; summarize search,
    graph, codewiki, indexing/projection, and review-hardening work.
  - **gobby-core (0.7.0):** shared foundation updates, including public API,
    feature, datastore, AI, setup, and progress-contract changes.
  - **gobby-hooks (0.7.0):** hook-dispatcher release work and the
    `gobby-core 0.7.0` floor.
  - **gwiki (0.7.0):** wiki ingestion, sync, search, compile, datastore,
    progress, and release-workflow updates.
  - **CI/CD:** nextest CI foundation, workflow action-pinning + checksum
    guardrails, per-asset SHA-256 in helper releases, legacy compatibility
    surface removal.

## Step 3 — Documentation sync

- **`README.md`** lines 28–35: version table → new versions + tags.
- **`docs/guides/release-guide.md`**: current release set table, version rules,
  tag order, and one-at-a-time push loop are already current; confirm the
  `_Last verified: 2026-07-01_` marker remains accurate.
- **`docs/guides/ghook-development-guide.md`**: example versions and asset URLs
  should stay at `0.7.0`.
- **`docs/guides/ghook-user-guide.md`**: example diagnostic output / asset URLs
  should stay at `0.7.0`.
- **`docs/guides/gcore-development-guide.md`**: dependency examples should stay
  at `0.7.0`.
- Verify root README install/`cargo install` snippets and per-crate READMEs need
  no change (they use `/releases/latest/` + version-less `cargo install` — confirmed).

## Step 4 — Local validation (before PR, to avoid a red CI)

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo nextest run --workspace --no-default-features
cargo test --doc --workspace --no-default-features
cargo build --release -p gobby-code -p gobby-hooks -p gobby-wiki
```

Optional pre-publish smoke: `cargo publish -p gobby-core --dry-run` (gobby-core
has no internal deps; dependents can't dry-run until 0.7.0 is on crates.io).

## Step 5 — Commit, push, PR, merge

- Single commit on `dev`:
  `release: gobby-core 0.7.0, gcode 1.4.0, ghook 0.7.0, gwiki 0.7.0`
  (Cargo.toml files, Cargo.lock, CHANGELOG.md, README.md, docs/guides/*).
- `git push origin dev`.
- `gh pr create --base main --head dev` with a release summary body.
- Wait for `ci.yml` (fmt/clippy per-feature, nextest per-crate, doctests,
  workspace build) to go green. CodeRabbit is advisory — surface any findings but
  don't block on them unless the user says so.
- `gh pr merge --merge` once CI passes. Then `git checkout main && git pull`.

## Step 6 — Tag + release cycle (autonomous, on merged main)

All tags point at main's merged HEAD. **gobby-core first**, wait for crates.io
indexing, then the rest **one tag per push** (GitHub suppresses push events when
>3 tags arrive at once — see release-guide):

```bash
git tag gobby-core-v0.7.0 && git push origin refs/tags/gobby-core-v0.7.0
# poll crates.io until gobby-core 0.7.0 is indexed (~30–60s)
for t in gcode-v1.4.0 ghook-v0.7.0 gwiki-v0.7.0; do
  git tag "$t" && git push origin "refs/tags/$t"
done   # gwiki last — its workflow re-verifies gobby-core's `ai` feature is published
```

Each binary workflow (`release: needs: [build, publish]`) only cuts the GitHub
release after crates.io publish succeeds. `release-gcore.yml` is publish-only.

## Verification

1. **Pre-tag:** local validation suite green (Step 4); `Cargo.lock` shows the four
   new versions; there are no stale non-`0.7.0` `gobby-core` pins in
   `crates/*/Cargo.toml`.
2. **CI:** PR `ci.yml` green before merge.
3. **Publish:** poll crates.io for each — e.g.
   `curl -s https://crates.io/api/v1/crates/gobby-core | jq '.crate.max_version'`
   → `0.7.0`; repeat for gobby-code 1.4.0, gobby-hooks 0.7.0, and
   gobby-wiki 0.7.0.
4. **GitHub releases:** `gh release list` shows all new tags with binary
   assets (gobby-core has no asset matrix — crates.io only).
5. **Workflow health:** `gh run list --workflow release-*.yml` — every release
   run concluded `success`. If gwiki failed on the gobby-core `ai`-feature probe,
   it raced indexing; re-run once core is confirmed indexed.
6. **Optional local install:** build + copy binaries into `~/.gobby/bin/` and
   refresh `.<name>-version` sidecars per release-guide "Local Install Check"
   (run `ghook --version` to rewrite `.ghook-runtime.json`).

## Risks / notes

- **Irreversible:** tag push publishes to crates.io (no unpublish, only yank) and
  cuts public GitHub releases. gcode 1.4.0 is a stable public release signal.
- **Ordering is load-bearing:** gobby-core must be indexed before the three
  dependents tag, or their `cargo publish` can't resolve `gobby-core 0.7.0`.
- If CI requires status checks that block direct merge, merge via the PR once
  green; tags are created on the resulting main commit regardless.
