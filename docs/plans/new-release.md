# Release prep: gcode 1.0.0, gwiki 0.3.0, gcore 0.4.0, ghook patch

## Context

Effectively the entire workspace has changed since the last published tags
(58 commits since `gcode-v0.9.9`, 52 since `gwiki-v0.2.0`). We're cutting a
coordinated release across active crates. `dev` is 52 commits ahead of
`origin/dev` and 63 ahead of `origin/main` with **zero divergence** (clean
fast-forward path to main).

Current Cargo.toml versions all match the last published tags, so the bumps are
clean. The one cross-cutting hazard: `gobby-core` 0.3.0 → 0.4.0 is a **breaking
0.x minor bump under Cargo semver**, so every dependent's `version = "0.3.0"`
pin on `gobby-core` must move to `"0.4.0"` or builds/publishes break.

Decisions (confirmed with user):
- **PR dev→main**, let `ci.yml` + CodeRabbit run, merge once CI is green
  (CodeRabbit treated as advisory).
- **Push tags autonomously** once main CI passes and local validation is clean,
  in dependency order — no pause.

## Target versions

| Crate dir | Package | Binary | Current → New | Tag |
|---|---|---|---|---|
| crates/gcode | `gobby-code` | gcode | `0.9.9` → **`1.0.0`** | `gcode-v1.0.0` |
| crates/gwiki | `gobby-wiki` | gwiki | `0.2.0` → **`0.3.0`** | `gwiki-v0.3.0` |
| crates/gcore | `gobby-core` | (lib) | `0.3.0` → **`0.4.0`** | `gobby-core-v0.4.0` |
| crates/ghook | `gobby-hooks` | ghook | `0.4.5` → **`0.4.6`** | `ghook-v0.4.6` |

Tag prefix convention (per project memory): short binary name for single-word
packages (`gcode-v*`), full package name for multi-word
(`gobby-core-v*`, `ghook-v*` → publishes `gobby-hooks`, `gwiki-v*` → `gobby-wiki`).

## Step 1 — Version bumps + gobby-core pin cascade

Edit the `version =` field (line 3) in each active crate's `Cargo.toml`:
`crates/gcode`, `crates/gwiki`, `crates/gcore`, `crates/ghook`.

**Critical cascade** — bump the `gobby-core` dependency pin from `version = "0.3.0"`
to `version = "0.4.0"` in every dependent (keep `path`/`features`/`default-features`
exactly as-is):
- `crates/gcode/Cargo.toml` (~line 25)
- `crates/gwiki/Cargo.toml` — **both** the dependency (~line 29) **and** the
  dev-dependency (~line 62)
- `crates/ghook/Cargo.toml` (~line 25)

Then regenerate the lockfile so the bumped versions land in `Cargo.lock`:
`cargo build --workspace --no-default-features` (commit the updated `Cargo.lock`).

## Step 2 — CHANGELOG.md

Format is Keep-a-Changelog with per-crate `#### <crate>` blocks under
`### Added/Changed/Fixed`, newest release set grouped just under `## [Unreleased]`.

- Fold the two existing `[Unreleased]` bullets (gcode grep `-w`, gwiki contract
  docs) into the new `[1.0.0] — gcode` / `[0.3.0] — gwiki` sections; leave
  `## [Unreleased]` as an empty heading.
- Insert four new version headings directly under `[Unreleased]`, above the
  current top entries:
  `## [1.0.0] — gcode`, `## [0.4.0] — gobby-core`, `## [0.3.0] — gwiki`,
  `## [0.4.6] — gobby-hooks`.
- Derive per-crate attribution from `git log gcode-v0.9.9..HEAD --stat` (bucket
  each commit by the crate dirs it touches). Summarize in the house style — do
  **not** transcribe all 58 commits:
  - **gcode (1.0.0):** frame as first stable release; new `grep -w/--word`
    identifier matching (breaking `-w` remap) + UUID exact-symbol resolution for
    `callers`/`usages`, `symbol-at` lookup, BM25 score primitive consolidation
    (`pdb` score fix), large-module decomposition (codewiki/db/import-resolution/
    cli split <1k lines), snapshot/property test coverage, review hardening.
  - **gobby-core (0.4.0):** shared module decomposition (config/provisioning
    split), datastore/AI adapter hardening, review hardening.
  - **gwiki (0.3.0):** research budget JSON (all five limits), genuine
    write-conflict stop, datastore contract enforcement, default-feature build
    fix, compile-contract key alignment, user-guide docs, Windows pdfium release
    builds + idempotent publish, large-module decomposition (research_loop/pdf/
    sources/compile/document/refresh/video split), fixture modernization.
  - **gobby-hooks (0.4.6):** aarch64-windows release target, review hardening,
    `gobby-core 0.4.0` floor.
  - **CI/CD:** nextest CI foundation, workflow action-pinning + checksum
    guardrails, per-asset SHA-256 in helper releases, legacy compatibility
    surface removal.

## Step 3 — Documentation sync

- **`README.md`** lines 28–35: version table → new versions + tags.
- **`docs/guides/release-guide.md`**: version table (7–14), "Version Rules"
  narrative (18–27), "Tag Order" `git tag` block + the one-at-a-time push loop
  (36–55) → new versions; bump `_Last verified_` date (line 124) to 2026-06-05.
- **`docs/guides/ghook-development-guide.md`**: example versions `0.4.4` →
  `0.4.6` (lines ~160–161, 358).
- **`docs/guides/ghook-user-guide.md`**: example diagnostic output / asset URLs
  `0.4.4` → `0.4.6` (lines ~204, 225).
- **`docs/guides/gcore-development-guide.md`**: dependency examples `0.2`/`0.2.2`
  → `0.4.0` (lines ~197, 205, 214, 223).
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
has no internal deps; dependents can't dry-run until 0.4.0 is on crates.io).

## Step 5 — Commit, push, PR, merge

- Single commit on `dev`:
  `release: gcode 1.0.0, gwiki 0.3.0, gcore 0.4.0, patch ghook`
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
git tag gobby-core-v0.4.0 && git push origin refs/tags/gobby-core-v0.4.0
# poll crates.io until gobby-core 0.4.0 is indexed (~30–60s)
for t in gcode-v1.0.0 ghook-v0.4.6 gwiki-v0.3.0; do
  git tag "$t" && git push origin "refs/tags/$t"
done   # gwiki last — its workflow re-verifies gobby-core's `ai` feature is published
```

Each binary workflow (`release: needs: [build, publish]`) only cuts the GitHub
release after crates.io publish succeeds. `release-gcore.yml` is publish-only.

## Verification

1. **Pre-tag:** local validation suite green (Step 4); `Cargo.lock` shows the four
   new versions; `grep -rn "0.3.0" crates/*/Cargo.toml` returns no stray
   `gobby-core` pins.
2. **CI:** PR `ci.yml` green before merge.
3. **Publish:** poll crates.io for each — e.g.
   `curl -s https://crates.io/api/v1/crates/gobby-core | jq '.crate.max_version'`
   → `0.4.0`; repeat for gobby-code 1.0.0, gobby-wiki 0.3.0, and
   gobby-hooks 0.4.6.
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
  cuts public GitHub releases. gcode 1.0.0 is a public API-stability signal.
- **Ordering is load-bearing:** gobby-core must be indexed before the three
  dependents tag, or their `cargo publish` can't resolve `gobby-core 0.4.0`.
- If CI requires status checks that block direct merge, merge via the PR once
  green; tags are created on the resulting main commit regardless.
