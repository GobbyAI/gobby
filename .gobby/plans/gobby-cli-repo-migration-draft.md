# Merge gobby-cli Into gobby — Phased Implementation Plan

## Context

`gcode`, `ghook`, `gwiki`, and `gobby-core` live in a separate Rust repo
(`GobbyAI/gobby-cli`), wired into the Python daemon (`GobbyAI/gobby`) via the
`deps/gobby-cli` submodule plus install/freshness code that fetches GitHub
Releases and a CI workflow that mirror-checks ghook schemas across repos. This
two-repo split is permanent operational tax: submodule bumps, cross-repo schema
mirroring, dual release wiring, and no way to land a Rust change and its Python
consumer in one commit.

Goal: make `GobbyAI/gobby` the canonical home of the Rust workspace under
`crates/`, preserving package/binary names, crate versions, per-crate release
tags, and `cargo install` UX (crates.io is unaffected). Then reduce
`gobby-cli` to a README-only, archived repo with all historical tags/releases
intact.

Decisions locked with the user:

- **History**: full history via `git-filter-repo` (blame/`log --follow` keep working).
- **Decommission**: full — README-only swap, disable issues/discussions, then
  archive, gated on a green first helper release from gobby.

### Verified current state

- Workspace = exactly 4 members: `crates/{gcode,gcore,ghook,gwiki}`; packages
  `gobby-code`/`gobby-hooks`/`gobby-wiki` + library `gobby-core`. (`gsqz`/`gloc`
  no longer exist — older memory was stale.)
- Submodule: `.gitmodules` → `deps/gobby-cli` tracking `main`. **Not** checked
  out in any gobby CI workflow; used only by local install fallback + tests.
- Release resolver `install_setup.py:_resolve_latest_release_tag` **already**
  filters releases by tag-prefix and skips drafts/prereleases → app `v*`
  releases will not be mistaken for helper binaries.
- gobby-cli release workflows use `gh release create … --repo "$GITHUB_REPOSITORY"`
  with **no** `make_latest` → moved as-is they would steal the repo "Latest" badge.
- `schema-mirror-check.yml` fetches `crates/ghook/schemas/{inbox-envelope.v1,
  diagnose-output.v1,diagnose-output.v2}.schema.json` from gobby-cli at a pinned
  SHA and sha256-compares to local `schemas/`. In-repo this becomes a same-repo
  check.
- gobby is pure-Python at root (no `Cargo.toml`/`.rs`); `.gitignore` already
  ignores `target/`. `ci.yml` has a "Classify Changes" job that runs full Python
  CI for any non-docs change.
- Publish ordering matters: `gobby-core-v*` must reach crates.io before
  `gwiki-v*` (the gwiki workflow already gates on `gobby-core/ai` being published).

---

## Phase 0 — Pre-flight (no code changes)

- Freeze `gobby-cli` main for the duration (announce; no new merges).
- Confirm the gobby repo's `CARGO_REGISTRY_TOKEN` secret exists and the
  publishing identity owns `gobby-code`, `gobby-hooks`, `gobby-wiki`,
  `gobby-core` on crates.io.
- Record current helper versions to validate post-merge:
  `gcode 1.3.3`, `ghook 0.6.2`, `gwiki 0.6.5`, `gobby-core 0.6.1`.
- Branch `merge-gobby-cli` off `main` in gobby for all of Phases 1–4.

**Gate:** secret present, ownership confirmed. gobby-cli untouched.

---

## Phase 1 — History merge (operator runbook, full history)

Rewrite gobby-cli history so only the Rust workspace + a few relocations remain,
then merge with full history attached under `crates/**`.

```bash
git clone --mirror https://github.com/GobbyAI/gobby-cli /tmp/gcli && cd /tmp/gcli
git filter-repo \
  --path crates/ --path Cargo.toml --path Cargo.lock \
  --path rust-toolchain.toml --path .config/nextest.toml \
  --path .github/workflows/release-gcode.yml \
  --path .github/workflows/release-ghook.yml \
  --path .github/workflows/release-gwiki.yml \
  --path .github/workflows/release-gcore.yml \
  --path .github/workflows/ci.yml \
  --path CHANGELOG.md \
  --path-rename CHANGELOG.md:crates/CHANGELOG.md \
  --path-rename .github/workflows/ci.yml:.github/workflows/rust-ci.yml
# in gobby:  merge-gobby-cli branch
git remote add gcli /tmp/gcli && git fetch gcli
git merge --allow-unrelated-histories gcli/main
git remote remove gcli
```

- **Dropped** (gobby's win): gobby-cli `README.md`, `CLAUDE.md`, `AGENTS.md`,
  `CONTRIBUTING.md`, `LICENSE` (both Apache-2.0), `.gitignore`,
  `.coderabbit.yaml`, `.markdownlint.json`, `.githooks/` (the gobby-cli pre-push
  that syncs a `gobby-wiki` vault to a `wiki` branch — must **not** come over),
  `.gobby/`, `.claude/`, `logo.png`, `built-with-gobby.svg`.
- **Relocated:** `CHANGELOG.md` → `crates/CHANGELOG.md`; gobby-cli `ci.yml` →
  `.github/workflows/rust-ci.yml` (avoids colliding with gobby's `ci.yml`). The
  four `release-*.yml` keep their names (no collision with gobby's `release.yml`).
- Remove the submodule: `git rm deps/gobby-cli`, delete `.gitmodules`.
- Reconcile root config: fold any needed Rust ignores into gobby's `.gitignore`
  (`target/` already present; add `Cargo` artifacts if missing). Add a Rust dev
  section to gobby's `CLAUDE.md`/`README.md` rather than importing gobby-cli's.

**Verify:** `cargo build --release -p gobby-code -p gobby-hooks -p gobby-wiki`
builds; `git log --follow crates/gcode/src/...` shows pre-merge commits; repo
root now has `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`, `crates/`.

**Gate:** workspace builds in-repo, history attached. gobby-cli still authoritative.

---

## Phase 2 — Repoint Python install / freshness / tests / schema mirror

All edits in gobby; behavior must keep working against gobby-cli releases until
Phase 4 cuts the first gobby release, so prefer in-repo-first with remote
fallback.

- `src/gobby/cli/install_setup.py`
  - `_build_release_download_url` (line ~82): `GobbyAI/gobby-cli` → `GobbyAI/gobby`.
  - `_resolve_latest_release_tag` (line ~96): API URL → `GobbyAI/gobby`.
    Tag-prefix filter stays as-is.
- `src/gobby/cli/install_setup_gcode.py` / `install_setup_gwiki.py` /
  `install_setup_ghook.py`
  - `--git https://github.com/GobbyAI/gobby-cli` → `GobbyAI/gobby`.
  - `install_*_from_submodule`: stop searching for `deps/gobby-cli/Cargo.toml`;
    resolve the **in-repo workspace root** (search parents for root `Cargo.toml`
    containing the workspace / `crates/<crate>/Cargo.toml`) and build
    `-p <package>` from it. Rename to reflect "from local workspace."
  - cargo-binstall (`gobby-code@<ver>`) and cargo-install (`gobby-code`) are
    crates.io — **no change**.
- `src/gobby/install/bin_freshness_updater.py` (+ `bin_freshness_models.py`,
  `version_pins.py`, `install_setup_versions.py`): audit for any independent
  GitHub owner/repo or releases URL; repoint to `GobbyAI/gobby`. Version pins and
  prefix-based selection logic stay.
- Schema mirror: convert `.github/workflows/schema-mirror-check.yml` to a
  **same-repo** check — sha256-compare `schemas/*.json` against
  `crates/ghook/schemas/*.json` directly; remove the network fetch and
  `schemas/SOURCE_COMMIT`. (Keeps the packaged `schemas/` mirror that ships in
  the wheel; eliminating the mirror entirely and reading `crates/ghook/schemas/`
  at runtime is a deferred cleanup, not this effort.)
- Test fixtures `tests/code_index/test_gcode_phase7_contract.py` and
  `tests/test_cli_contracts.py`: resolve the in-repo `crates/` path first; keep
  `$GOBBY_CLI_REPO` / `~/Projects/gobby-cli` as a transitional fallback so the
  suite is green during migration.
- Docs: replace "sister repo"/`gobby-cli` wording in live install/release/dev
  docs with "included Rust workspace under `crates/`." Historical/architecture
  docs describing the past split may keep old references.

**Verify (Python focused — never the full suite):**
```bash
GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/test_install_setup.py tests/cli/test_install_ghook.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_gcode_phase7_contract.py -v
GOBBY_TEST_PROTECT=1 uv run pytest tests/install/test_distribution.py -v
```

**Gate:** focused Python tests green; schema check passes same-repo.

---

## Phase 3 — CI integration with path filters

Avoid every PR running both toolchains, and never let missing Rust coverage
touch the 80% Python gate.

- `ci.yml` "Classify Changes": make it three-way — `crates/**`, `Cargo.toml`,
  `Cargo.lock`, `rust-toolchain.toml` ⇒ **rust-only** (skip the Python full-CI +
  80% coverage job); `src/**`/Python paths ⇒ skip the Rust job; mixed ⇒ both.
- `rust-ci.yml` (the ported gobby-cli CI): trigger only on the Rust paths above.
  It carries fmt, the per-crate clippy/test matrix (incl. `--no-default-features`
  legs), `cargo nextest run --profile ci`, doctests, and the
  `gcode-graph-standalone` job (PostgreSQL `pg_search` + FalkorDB services).
- Keep `postgres-pgsearch-smoke.yml` / `pre-push-test.sh` / `.pre-commit-config.yaml`
  **Python/TS-only**. Rust validation is a separate, path-aware lane — do not bolt
  `cargo` onto the 15k-test pre-push.
- Do not add `submodules: recursive` anywhere (submodule is gone).

**Verify:** a crates-only PR runs `rust-ci` and skips Python full CI; a src-only
PR skips `rust-ci`; coverage gate still 80% on Python changes.

**Gate:** both lanes green and correctly skipped.

---

## Phase 4 — Release / distribution flip (in gobby)

- Per-crate `Cargo.toml` `repository =` → `https://github.com/GobbyAI/gobby`
  (homepage `https://gobby.ai` unchanged). No package renames; versions unchanged.
- In each helper workflow (`release-gcode.yml`, `release-ghook.yml`,
  `release-gwiki.yml`, `release-gcore.yml`), add **`--latest=false`** to the
  `gh release create` call so helper tags never steal the repo "Latest" badge
  from the app's `v*` release. (`release-gcore.yml` has no GitHub-release job —
  publish-only — so only the three binary workflows need the flag.)
- Preserve publish ordering: `gobby-core-v*` first; gwiki's pre-publish check
  already verifies `gobby-core/ai` on crates.io.
- Confirm pdfium provisioning in `release-gwiki.yml` carries over intact.
- **First-release validation (low risk → real):**
  1. `release-gwiki.yml` `workflow_dispatch` dry-run (builds incl. windows-msvc,
     skips publish/release) — proves the workflow runs in gobby.
  2. Cut a real low-risk tag (e.g. a `ghook` patch) → verify: build → crates.io
     publish → GitHub release asset on `GobbyAI/gobby` → app "Latest" badge still
     points at `v*`.
  3. Run a clean `gobby install --hooks --no-interactive --no-ext-services` in an
     isolated HOME and confirm gcode/ghook/gwiki resolve from `GobbyAI/gobby`
     releases (and `_resolve_latest_release_tag` returns the right tag).
- Homebrew tap (`GobbyAI/homebrew-tap`): repoint helper-formula `url`/source tags
  and the `repository_dispatch` `helper_repo` from `gobby-cli` → `gobby`; run tap
  CI smoke. Historical formula versions still resolve (archived repos serve
  tarballs).

**Gate:** a real helper release from gobby is green end-to-end and installers
resolve from gobby. **This is the gate for Phase 5.**

---

## Phase 5 — Full validation

- **Python:** the three focused commands from Phase 2.
- **Rust:**
  ```bash
  cargo fmt --all --check
  cargo clippy --workspace --all-targets -- -D warnings
  cargo nextest run --workspace --no-default-features
  cargo test --doc --workspace --no-default-features
  cargo build --release -p gobby-code -p gobby-hooks -p gobby-wiki
  ```
- **Release/distribution:** installer URL resolution against `GobbyAI/gobby`
  releases; schema-mirror same-repo check passes; gwiki dry-run dispatch green.
- **History sanity:** `git log --follow` on a representative file in each crate.

---

## Phase 6 — Decommission gobby-cli (gated on Phase 4)

Only after a confirmed green helper release from gobby + installer verification:
1. Replace gobby-cli `main` contents with a single `README.md` (normal commit,
   **not** a history rewrite) stating: active source →
   `https://github.com/GobbyAI/gobby/tree/main/crates`, new releases →
   `https://github.com/GobbyAI/gobby/releases`, historical gobby-cli releases
   remain available, and install commands are unchanged
   (`cargo install gobby-code` / `gobby-hooks` / `gobby-wiki`).
2. Disable issues + discussions on gobby-cli.
3. Archive gobby-cli (reversible). Historical tags + release assets preserved.

**Rollback:** through Phase 5, gobby-cli is untouched and remains the
authoritative fallback; revert workflow enablement to back out. Phase 6 archive
is reversible via unarchive.

---

## Critical files

| File | Change |
| --- | --- |
| `.gitmodules`, `deps/gobby-cli/` | Removed |
| root `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`, `crates/**` | Added (merge) |
| `src/gobby/cli/install_setup.py` | `_build_release_download_url`, `_resolve_latest_release_tag` → `GobbyAI/gobby` |
| `src/gobby/cli/install_setup_{gcode,gwiki,ghook}.py` | `--git` URL → gobby; `*_from_submodule` → in-repo workspace |
| `src/gobby/install/bin_freshness_updater.py`, `version_pins.py`, `install_setup_versions.py` | Audit + repoint any own GitHub URL |
| `.github/workflows/schema-mirror-check.yml`, `schemas/SOURCE_COMMIT` | Convert to same-repo check; drop SOURCE_COMMIT |
| `.github/workflows/ci.yml` | Three-way classifier (rust-only / python-only / both) |
| `.github/workflows/rust-ci.yml` | Ported gobby-cli CI, path-filtered |
| `.github/workflows/release-{gcode,ghook,gwiki,gcore}.yml` | Enabled in gobby; `--latest=false` on the 3 binary workflows |
| `crates/{gcode,gcore,ghook,gwiki}/Cargo.toml` | `repository` → gobby |
| `tests/code_index/test_gcode_phase7_contract.py`, `tests/test_cli_contracts.py` | Resolve in-repo `crates/` first |

## Out of band (operator / GitHub admin, not auto-executable here)
filter-repo history surgery; pushing helper tags; crates.io publishes; the
`CARGO_REGISTRY_TOKEN` secret; Homebrew tap edits; disabling issues/discussions;
archiving gobby-cli.
