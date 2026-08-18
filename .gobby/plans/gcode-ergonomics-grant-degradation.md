# gcode CLI Ergonomics and Typed Grant Degradation

> **Plan ID:** gcode-ergonomics-grant-degradation

## Overview
`kind: framing`

Planning sub-epic #20454 (parent epic #20442). Transcript audits show gcode burning
agent context and trust in four ways: ripgrep idioms (`-E`, `-l`, `-m`, `-w` in rg
positions) rejected with full clap usage dumps (26 unexpected-argument failures in one
grok session); nonzero exits on success-shaped calls breaking `&&` chains (6x observed);
agents defensively appending the removed `--no-freshness` flag; and grant/payload skew
surfacing as an opaque `malformed grant` serde dump (the 08-17 `credential_generation`
rollout window killed all 4 spawned codex reviewers for ~2.5h while interactive sessions
coasted on cached grants). The drafting session for this plan reproduced the skew live:
the managed-execution bootstrap written by the running daemon carries a top-level
`credential_generation` field, and every gcode binary built from this branch rejects it
with the raw serde message `unknown field 'credential_generation' … at line 1 column 24`
— no self-diagnosis, no recovery directive.

The plan grounds on the local 0.5.0 branch state. On this branch `gcode grep` already
accepts `-w` and `-m` (commit `bffdc153fc` renamed the freshness bypass to
`--allow-stale`), so the remaining ergonomics work is the `-E`/`-l` class of rg idioms,
quiet argument errors, a pinned exit-code contract, freshness-failure degradation, typed
grant skew errors on every acquisition path, and accurate recovery-directive text.

## Constraints
`kind: framing`

- Rust workspace: `gobby-code` (`crates/gcode`) and `gobby-core` (`crates/gcore`).
  Binaries go live only after rebuild **and** reinstall to `~/.gobby/bin/` via a new
  inode (`cp` to a dotfile, `mv -f` over the name); a committed crate change is not
  delivered behavior until then.
- No backward compatibility: 0.5.0 has not shipped. Existing "unsupported grep/rg flags
  are intentionally rejected" tests and after-help text are deliberately reversed for
  the bounded compat set below; strict rejection remains for flags with no sane
  indexed-grep meaning, but with concise rendering.
- `EXPECTED_API_CONTRACT` / `API_CONTRACT` stay at 1 in this epic. The
  `credential_generation` payload change already shipped without a bump; bumping now
  cannot help binaries that fail deserialization before the contract gate. The lasting
  fix is the two-stage parse (P3) plus cross-language drift enforcement (3.4) so the
  next payload change forces a coordinated bump.
- Monolith ceiling: `crates/gcode/src/commands/grep.rs` (930 lines, inline tests count)
  and `crates/gcore/src/grant/mod.rs` (990 lines) sit near the 1,000-line ceiling. 1.1
  moves the inline grep test module out; 3.1 extracts the cached-grant inspection
  surface. Both are specified inside the owning deliverable, not deferred.
- Never run the full pytest suite; validation is scoped (`cargo test -p gobby-code`,
  `cargo test -p gobby-core`, focused pytest files). Prefix agent pytest runs with
  `GOBBY_TEST_PROTECT=1`.
- Rule/skill templates under `src/gobby/install/shared/` sync to DB registry rows on
  daemon start when definitions drift; the DB row is what enforces. No extra sync work
  is needed beyond editing templates, but verification must observe the refreshed rows.

## P1: Ripgrep compatibility and quiet flag errors
`kind: framing`

**Goal**: `gcode grep` accepts the audited ripgrep idioms, and every argument error
costs the agent three lines instead of a full usage dump.

### 1.1 Add ripgrep-idiom flags to gcode grep [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/cli.rs::Command`
- `crates/gcode/src/commands/grep.rs::GrepOptions`
- `crates/gcode/src/commands/grep.rs::run`
- `crates/gcode/src/commands/grep.rs::GrepResponse`
- `crates/gcode/src/commands/grep.rs::format_text_matches`
- `crates/gcode/src/commands/grep/tests.rs`
- `crates/gcode/src/commands/grep/db_tests.rs::grep_scopes_chunks_to_local_machine_file_state`
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/codewiki_facts/text.rs::CodewikiFacts::grep_with`
- `crates/gcode/src/contract.rs::grep_flags`
- `crates/gcode/src/cli/tests/grep.rs::*` — scope-reason: rejection tests for `-l`/`-n` are replaced by acceptance tests and new parse tests are added across the module

Extend the `Grep` variant of the `Command` enum in `crates/gcode/src/cli.rs` with:

```rust
/// List matching file paths instead of matching lines (rg -l)
#[arg(short = 'l', long)]
files_with_matches: bool,
/// Accepted no-op: patterns are already extended regex (grep/rg -E)
#[arg(short = 'E', long = "extended-regexp", hide = true)]
extended_regexp: bool,
/// Accepted no-op: line numbers are always shown (grep/rg -n)
#[arg(short = 'n', long = "line-number", hide = true)]
line_number: bool,
/// Accepted no-op: indexed grep is always recursive (grep -r/-R)
#[arg(short = 'r', long = "recursive", hide = true)]
recursive: bool,
#[arg(short = 'R', hide = true)]
recursive_dereference: bool,
```

Semantics:

- The four no-op switches parse and are ignored; they are `hide = true` so `--help`
  never advertises them. They exist purely so `grep -rn`, `grep -E 'a|b'`, and
  `rg -n` muscle memory does not cost an agent a failed call.
- `-l/--files-with-matches` is a real output mode. `GrepOptions` gains
  `files_with_matches: bool`; `run` computes matches exactly as today, then:
  - text mode: print sorted unique matching paths, one per line (rg parity);
  - JSON mode: `GrepResponse` gains `#[serde(skip_serializing_if = "Option::is_none")]
    files: Option<Vec<String>>` holding the sorted unique paths while `matches` stays
    empty; `matched_lines` still reports the total matching-line count.
  - `-m N` caps the number of files listed (deterministic, sorted; a documented
    divergence from rg's per-file cap) and sets `truncated` when it bites.
  - Context flags (`-A`/`-B`/`-C`) are silently ignored with `-l` (rg parity).
- Update the `Grep` after-help in `crates/gcode/src/cli.rs` — it currently says
  unsupported grep/rg flags are intentionally rejected; rewrite to name the accepted
  set (`-F -i -w -l -m -A -B -C -g` plus accepted no-ops `-E -n -r -R`) and point
  remaining gaps at raw `rg`.
- `dispatch.rs::run` forwards the new flags into `GrepOptions`; the struct literals in
  `CodewikiFacts::grep_with` and the grep db test set `files_with_matches: false`.
- `contract.rs::grep_flags` adds `--files-with-matches` and the no-op switches (the
  pinned contract JSON regeneration happens once, in 2.2).
- Ceiling work (same deliverable, same session): move the inline
  `#[cfg(test)] mod tests` block out of `crates/gcode/src/commands/grep.rs` into a new
  `crates/gcode/src/commands/grep/tests.rs` declared with
  `#[cfg(test)] #[path = "grep/tests.rs"] mod tests;`, keeping `grep.rs` well under
  1,000 lines after the `-l` additions.
- Replace the `parse_grep_rejects_line_number_flag` and
  `parse_grep_unsupported_flag_fails_before_context_resolution` /
  `parse_grep_unsupported_flag_after_path_fails_in_clap` expectations in
  `crates/gcode/src/cli/tests/grep.rs`: `-n` and `--files-with-matches` now parse; add
  parse tests for `-l`, `-E`, `-r`, and `-l` composed with `-m` and paths.

**Acceptance:**

- 1.1.1 - `gcode grep -l` lists sorted unique matching paths in text mode and populates `files` in JSON mode. symbol: `GrepResponse`. file: `crates/gcode/src/commands/grep.rs`.
- 1.1.2 - `-E`, `-n`, `-r`, and `-R` parse as hidden accepted no-ops on the grep subcommand. symbol: `Command`. file: `crates/gcode/src/cli.rs`.
- 1.1.3 - Grep CLI parse tests cover `-l`, the no-op switches, and `-l` with `-m`/paths, replacing the old rejection tests. test: `crates/gcode/src/cli/tests/grep.rs`.
- 1.1.4 - The inline grep test module lives in its own file and `commands/grep.rs` stays under the monolith ceiling. file: `crates/gcode/src/commands/grep/tests.rs`.
- 1.1.5 - The grep contract flag list includes the new switches. symbol: `grep_flags`. file: `crates/gcode/src/contract.rs`.

### 1.2 Replace clap usage dumps with concise argument errors [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/dispatch.rs::run`
- `crates/gcode/src/dispatch.rs::run_with_exit_code`
- `crates/gcode/src/dispatch/usage.rs`
- `crates/gcode/src/cli_error.rs::CliError`
- `crates/gcode/src/cli/tests/top_level.rs::*` — scope-reason: usage-rendering assertions are added and the removed-flag tests are repointed at the concise error shape

Today `dispatch.rs::run` calls `Cli::parse()`, so any unknown argument makes clap print
its full error (which for top-level mistakes includes the entire 30-command listing)
and exit 2. Replace it with `Cli::try_parse()` plus a concise renderer in a new
`crates/gcode/src/dispatch/usage.rs` submodule (declared from `dispatch.rs` with
`mod usage;` — no `lib.rs` change):

- `ErrorKind::DisplayHelp` / `DisplayVersion`: print clap's output unchanged to stdout,
  exit 0 (help stays help).
- Every other parse error becomes a `CliError { code: "usage", message, recovery,
  exit_status: 2 }` printed as one JSON line on stderr, matching the typed-error shape
  agents already parse. `message` is clap's first error line plus the one-line
  `Usage:` string extracted from clap's rendered error (never the command listing);
  `recovery` comes from a static suggestion table keyed on the offending token:

  | foreign flag | recovery |
  | --- | --- |
  | `--limit` | use `-m`/`--max-count` |
  | `-e` / `--regexp` | pass the pattern as the first positional argument |
  | `-c` / `--count` | use `--format json` and read `matched_lines` |
  | `-t` / `--type` | use `-g` with a glob such as `-g '*.py'` |
  | `--no-freshness` | use `--allow-stale` |
  | `-S` / `--smart-case` | use `-i` |
  | `-v` / `--invert-match` | not supported by indexed grep; use raw `rg` |

- `CliError` in `crates/gcode/src/cli_error.rs` gains
  `recovery: Option<&'static str>` (also consumed by 3.3); `CliError::print` emits it
  as a `recovery` key when present.
- `run_with_exit_code` needs no new arm: the usage error is a `CliError` and follows
  the existing typed-error path (stderr JSON, exit 2).
- Total stderr cost of any argument error: one JSON line. Unit tests in `usage.rs`
  (inline `#[cfg(test)]`) assert the rendered shape for an unknown grep flag, an
  unknown top-level subcommand, and a `--no-freshness` suggestion; the
  `test_rejects_removed_no_freshness_global_flag` expectations in
  `crates/gcode/src/cli/tests/top_level.rs` are repointed at `try_parse` still failing
  (the flag stays removed — only the rendering changes).

**Acceptance:**

- 1.2.1 - Argument errors print one JSON line with `error: "usage"`, a single usage string, and a recovery hint; no command listing is emitted. file: `crates/gcode/src/dispatch/usage.rs`.
- 1.2.2 - `CliError` carries an optional recovery directive rendered into the stderr JSON. symbol: `CliError`. file: `crates/gcode/src/cli_error.rs`.
- 1.2.3 - Help and version requests keep clap's full output on stdout with exit 0. test: `crates/gcode/src/cli/tests/top_level.rs`.

## P2: Exit-code contract and freshness degradation
`kind: framing`

**Goal**: exit codes are a pinned, machine-readable contract — 0 always means success
(empty results included), and a failed freshness side-trip can no longer fail a read
that the index can still serve.

### 2.1 Degrade read-time freshness failures to warnings [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/freshness.rs::ensure_fresh`
- `crates/gcode/src/freshness.rs::FreshnessStatus`
- `crates/gcode/src/dispatch.rs::warn_if_busy`
- `crates/gcode/src/dispatch.rs::ensure_project_fresh`
- `crates/gcode/src/dispatch.rs::ensure_files_fresh`
- `crates/gcode/src/dispatch.rs::ensure_symbol_fresh`

Read commands (`search*`, `grep`, `outline`, `symbol*`, `tree`, `kinds`, graph reads)
run `freshness::ensure_fresh` before serving. Today a failure inside the re-index
closure propagates as a generic anyhow error: the read dies with exit 1 and the raw
error even though the indexed data is still fully servable. This is the concrete
mechanism that taught agents to spray `--allow-stale` (and the removed
`--no-freshness`) on every call.

Change `ensure_fresh` so the incremental `api::index_files` refresh failing inside
`with_project_lock` yields a new `FreshnessStatus::Degraded(String)` (the error text)
instead of `Err`. Fatal remains fatal: hub-connection and DB errors from
`project_needs_refresh` or lock acquisition still propagate, because the read itself
would fail on the same hub. `ensure_symbol_fresh` inherits the behavior through its
`ensure_fresh` tail call.

Extend `warn_if_busy` in `dispatch.rs` (rename to `warn_freshness`) to print, unless
`--quiet`:

```text
warning: index refresh failed (<error>); serving existing index (pass --allow-stale to skip this check)
```

for `Degraded`, keeping the existing `SkippedBusy` message. The three `ensure_*`
helpers in `dispatch.rs` route the new status through it. Command output and exit code
are unaffected: the read serves indexed data and exits 0.

Add freshness unit tests beside the existing `serial_db` module in
`crates/gcode/src/freshness.rs` covering: refresh-closure failure yields `Degraded`
(e.g. unreadable project root inside the closure), and hub-connect failure still
errors.

**Acceptance:**

- 2.1.1 - A failed incremental refresh yields `FreshnessStatus::Degraded` and the read command still succeeds with exit 0. symbol: `ensure_fresh`. file: `crates/gcode/src/freshness.rs`.
- 2.1.2 - Degraded freshness prints a single stderr warning naming `--allow-stale`, suppressed by `--quiet`. symbol: `warn_if_busy`. file: `crates/gcode/src/dispatch.rs`.
- 2.1.3 - Hub/DB unavailability still fails the command (fail-closed path unchanged). test: `crates/gcode/src/freshness.rs`.

### 2.2 Pin the gcode exit-code contract [category: code] (depends: 1.2, 2.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/cli_contract.rs::CliContract`
- `crates/gcode/src/contract.rs::contract`
- `crates/gwiki/src/contract.rs::contract`
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerated pinned contract baseline
- `crates/gcode/tests/contract.rs::contract_is_version_four_without_codewiki`
- `crates/gcode/src/dispatch/tests.rs::*` — scope-reason: new exit-classification tests added across the module

The audited "exit 1 on success" incidents predate `f036f42d74` and the current branch;
no success path in current source returns nonzero, and 2.1 removes the last
success-shaped nonzero (freshness side-trips). What is missing is a pinned contract so
regressions cannot ship silently. Pin this table:

| exit | meaning |
| --- | --- |
| 0 | success, including empty result sets |
| 1 | internal error (unclassified bug); plain `Error:` line on stderr |
| 2 | usage error or typed error (grant, project_required, capability_unavailable, graph sync contract); one JSON line on stderr |
| 3 | `index --skip-if-locked` yielded to a concurrent indexer |
| 10 | embeddings doctor: config missing |
| 11 | embeddings doctor: config drift |
| 20 | embeddings doctor: transport failure |

Work:

- Extend `CliContract` in `crates/gcore/src/cli_contract.rs` with
  `pub exit_codes: Vec<ExitCodeContract>` and
  `pub struct ExitCodeContract { pub code: u8, pub meaning: &'static str }`.
- Populate the table in `crates/gcode/src/contract.rs::contract` and bump
  `contract_version` to 5; give `crates/gwiki/src/contract.rs::contract` its own
  accurate `exit_codes` (0/1/2 subset) since the struct field is mandatory.
- Regenerate the pinned baseline `crates/gcode/contract/gcode.contract.json` from the
  builder output and repoint the
  `contract_is_version_four_without_codewiki` assertions in
  `crates/gcode/tests/contract.rs` at version 5 plus the new `exit_codes` block
  (`--no-freshness` absence assertions stay).
- New tests in `crates/gcode/src/dispatch/tests.rs` pin `run_with_exit_code`
  classification: a `CliError` exits with its `exit_status`, a `GrantError` maps
  through `CliError::grant`, and an unclassified anyhow error exits 1. Grep/search
  empty-result exit-0 behavior is asserted at the command layer in the existing grep
  test surface (`GrepResult` with zero matches returns `Ok`).

**Acceptance:**

- 2.2.1 - The CLI contract carries a typed exit-code table. symbol: `CliContract`. file: `crates/gcore/src/cli_contract.rs`.
- 2.2.2 - `gcode contract` emits `contract_version` 5 with the exit-code table, and the pinned JSON baseline matches the builder. file: `crates/gcode/contract/gcode.contract.json`.
- 2.2.3 - Exit-classification tests pin typed-error exits at 2 and reserve 1 for unclassified internal errors. test: `crates/gcode/src/dispatch/tests.rs`.
- 2.2.4 - gwiki's contract builder compiles with and declares its own exit-code table. symbol: `contract`. file: `crates/gwiki/src/contract.rs`.

## P3: Typed grant degradation on every acquisition path
`kind: framing`

**Goal**: grant/payload skew self-diagnoses. Every acquisition path — managed file,
interactive cache, daemon handshake, offline load — reports a typed error that names
the skew, the source artifact, and the recovery action, instead of a raw serde dump.

### 3.1 Two-stage grant parse with typed api-contract and payload-skew errors [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/src/grant/bundle.rs::parse_grant_json`
- `crates/gcore/src/grant/mod.rs::GrantError`
- `crates/gcore/src/grant/mod.rs::GrantError::cli_code`
- `crates/gcore/src/grant/mod.rs::GrantError::exit_status`
- `crates/gcore/src/grant/inspection.rs`
- `crates/gcore/src/grant/mod.rs::inspect_cached_grant`
- `crates/gcore/src/grant/mod.rs::inspect_cached_grant_at`
- `crates/gcore/src/grant/mod.rs::CachedGrantInspection`
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: new two-stage-parse and skew-variant contract tests added across the module

Root cause: `GrantBundle` and every nested capability type carry
`#[serde(deny_unknown_fields)]`, so a daemon-side payload addition kills strict
deserialization *before* `validate_for_construction` can report
`ApiContractMismatch`. Skew can only ever surface as
`GrantError::Malformed(<raw serde text>)`.

Rework `parse_grant_json` in `crates/gcore/src/grant/bundle.rs` into a two-stage
parse:

```rust
#[derive(serde::Deserialize)]
struct GrantProbe {
    version: Option<i64>,
    api_contract: Option<i64>,
}
```

- Stage 1: tolerant `GrantProbe` parse (unknown fields ignored). Raw non-JSON input
  stays `Malformed`. `probe.api_contract != Some(EXPECTED_API_CONTRACT)` returns the
  enriched `ApiContractMismatch` (below). This also covers the live incident shape —
  the flat managed-bootstrap file has no `api_contract` key at all and now reports a
  typed contract mismatch instead of an unknown-field dump.
- Stage 2: today's strict parse plus checksum verification. A strict-parse failure
  whose serde message contains `unknown field` while stage 1 passed becomes the new
  `PayloadSkew` — the contract number matches but the payload shape does not, meaning
  daemon and binary are from different builds.

`GrantError` in `crates/gcore/src/grant/mod.rs` changes:

```rust
#[error("grant api contract {grant_contract:?} does not match this binary's supported contract {binary_contract}")]
ApiContractMismatch { grant_contract: Option<i64>, binary_contract: i64 },
#[error("grant payload skew: {detail}")]
PayloadSkew { detail: String },
```

- `cli_code`: `ApiContractMismatch { .. }` keeps `"api_contract_mismatch"`;
  `PayloadSkew` maps to `"payload_skew"`. Both keep `exit_status` 2.
- `validate_for_construction` constructs the enriched `ApiContractMismatch` from the
  parsed bundle's `api_contract`.
- No other crate matches on `ApiContractMismatch` outside `gcore`'s grant module and
  its tests, so the variant reshape is contained.
- Ceiling work (same deliverable): extract `CachedGrantInspection`,
  `inspect_cached_grant`, and `inspect_cached_grant_at` from `grant/mod.rs` into a new
  `crates/gcore/src/grant/inspection.rs` submodule, re-exported from `mod.rs`
  unchanged, keeping `mod.rs` under the 1,000-line ceiling after the new variants.
- New tests in `crates/gcore/src/grant/tests.rs`: a fixture grant with an injected
  unknown top-level field parses to `PayloadSkew` with cli code `payload_skew` and
  exit 2; a fixture with `api_contract: 99` and one with the key absent both parse to
  the enriched `ApiContractMismatch`; the existing `api_contract_gate` test updates to
  the struct variant.

**Acceptance:**

- 3.1.1 - Unknown grant fields with a matching contract number parse to `PayloadSkew`, never a raw serde dump. symbol: `parse_grant_json`. file: `crates/gcore/src/grant/bundle.rs`.
- 3.1.2 - A wrong or absent `api_contract` reports the enriched typed mismatch carrying both contract numbers. symbol: `GrantError`. file: `crates/gcore/src/grant/mod.rs`.
- 3.1.3 - `payload_skew` and `api_contract_mismatch` cli codes both exit 2. test: `crates/gcore/src/grant/tests.rs`.
- 3.1.4 - The cached-grant inspection surface lives in its own submodule with unchanged re-exports. file: `crates/gcore/src/grant/inspection.rs`.

### 3.2 Source-annotated acquisition errors and inspection reasons [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcore/src/grant/cache.rs::parse_cache_bytes`
- `crates/gcore/src/grant/cache.rs::load_grant_file`
- `crates/gcore/src/grant/mod.rs::acquire_managed`
- `crates/gcore/src/grant/mod.rs::acquire_interactive`
- `crates/gcore/src/grant/mod.rs::refresh_or_fail`
- `crates/gcore/src/grant/mod.rs::rehandshake`
- `crates/gcore/src/grant/handshake.rs::grant_from_handshake`
- `crates/gcore/src/grant/inspection.rs`
- `crates/gwiki/src/commands/status.rs::grant_status_snapshot`
- `crates/gwiki/src/commands/status.rs::load_cached_grant_bundle`
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: per-path source-annotation tests added across the module

A typed skew error is only self-diagnosing if it names which artifact is skewed. Add a
small helper in `grant/mod.rs`:

```rust
fn annotate_source(error: GrantError, source: &str) -> GrantError
```

that prefixes `source` into the message-bearing variants (`Malformed`, `PayloadSkew`)
and leaves every other variant untouched. Apply it on each acquisition path:

- managed file: `acquire_managed` and the managed branch of `rehandshake` annotate
  `load_grant_file` errors with `managed grant file <path>`;
- interactive cache: `acquire_interactive`'s `inspect_cache_pair` errors annotate with
  `interactive grant cache <path>`;
- handshake: `grant_from_handshake` in `handshake.rs` annotates parse errors with
  `daemon handshake response`;
- offline load: the `load_grant_file` re-read inside `refresh_or_fail` annotates with
  `cached grant <path>`.

Also fix the misleading cache fallback in `parse_cache_bytes`
(`crates/gcore/src/grant/cache.rs`): today a coherent-envelope file whose inner grant
has an unknown field falls through and is re-parsed as a *bare* grant, producing an
error about the envelope's own keys. Probe the raw JSON for a top-level `"grant"` key
first: envelope-shaped input routes the inner `grant` value through `parse_grant_json`
(getting the 3.1 typed errors) and never falls back to the bare-grant path.

`CachedGrantInspection::Malformed` (now in `grant/inspection.rs`) becomes
`Malformed { reason: String }` so status surfaces can show *why*;
`grant_status_snapshot` and `load_cached_grant_bundle` in
`crates/gwiki/src/commands/status.rs` render the reason in the existing status output.

Tests in `crates/gcore/src/grant/tests.rs` cover one annotated error per acquisition
path (managed file, cache, handshake response, offline re-read) plus the
envelope-with-skewed-grant cache case.

**Acceptance:**

- 3.2.1 - Every acquisition path's parse failure names its source artifact in the typed error. symbol: `acquire_managed`. file: `crates/gcore/src/grant/mod.rs`.
- 3.2.2 - Envelope cache files with a skewed inner grant report the inner grant's typed error, not a bare-grant reparse. symbol: `parse_cache_bytes`. file: `crates/gcore/src/grant/cache.rs`.
- 3.2.3 - Cached-grant inspection reports a malformed reason and gwiki status renders it. symbol: `grant_status_snapshot`. file: `crates/gwiki/src/commands/status.rs`.
- 3.2.4 - Per-path annotation behavior is pinned by tests. test: `crates/gcore/src/grant/tests.rs`.

### 3.3 Recovery directives on gcode grant errors [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/cli_error.rs::CliError::grant`
- `crates/gcode/src/cli_error.rs::CliError::print`

Using the `recovery` field added in 1.2, `CliError::grant` populates a static
directive per grant error class so the stderr JSON self-diagnoses:

| cli code | recovery |
| --- | --- |
| `payload_skew`, `api_contract_mismatch` | rebuild and reinstall the ~/.gobby/bin binaries (`gobby install`), or restart the Gobby daemon that matches them |
| `daemon_required` | start the Gobby daemon (`gobby start`) |
| `expired`, `revoked` | re-run the command after the daemon reissues the grant; if it persists, restart the session |
| `schema_mismatch`, `deployment_mismatch`, `config_revision_mismatch` | restart the Gobby daemon so grants match the installed schema and config |
| others (`timeout`, `malformed`, `io`, `remote_endpoint`) | no directive (message stands alone) |

The 08-17 outage shape becomes, end to end:

```json
{"error":"api_contract_mismatch","message":"managed grant file <path>: grant api contract None does not match this binary's supported contract 1","recovery":"rebuild and reinstall the ~/.gobby/bin binaries (gobby install), or restart the Gobby daemon that matches them"}
```

Unit tests in `cli_error.rs` (inline `#[cfg(test)]`) pin the JSON shape for a
`PayloadSkew` and a `DaemonRequired` conversion.

**Acceptance:**

- 3.3.1 - Grant skew errors printed by gcode carry the reinstall/restart recovery directive. symbol: `CliError::grant`. file: `crates/gcode/src/cli_error.rs`.
- 3.3.2 - The stderr JSON includes `recovery` only when a directive exists. symbol: `CliError::print`. file: `crates/gcode/src/cli_error.rs`.

### 3.4 Cross-language payload-drift enforcement tests [category: test] (depends: 3.1)
`kind: deliverable`

Targets:
- `tests/runtime_grants/golden/payload_skew_unknown_field.json`
- `tests/runtime_grants/test_golden_vectors.py::*` — scope-reason: new drift-enforcement and skew-vector tests added across the module
- `crates/gcore/src/grant/tests.rs::*` — scope-reason: Rust twins of the drift-enforcement tests added across the module

Make the next `credential_generation`-style change impossible to ship silently:

- New golden vector `tests/runtime_grants/golden/payload_skew_unknown_field.json`: the
  `direct_datastores.json` grant plus an injected unknown top-level field
  (`"future_capability_probe": 1`), same `api_contract`. Rust asserts
  `parse_grant_json` yields `PayloadSkew`; Python asserts the pydantic `GrantBundle`
  (`extra="forbid"`) rejects it. Both sides prove the same wire bytes fail closed with
  the same classification.
- Field-inventory pin, both languages: a Rust test asserting the sorted serialized
  field-name inventory of a fixture `GrantBundle` (top level plus
  `capabilities.postgres` direct variant) equals a pinned list, and a Python twin in
  `tests/runtime_grants/test_golden_vectors.py` asserting the pydantic model fields
  equal the same list. Each pin carries the comment: changing this inventory requires
  bumping `EXPECTED_API_CONTRACT` (Rust) and `API_CONTRACT` (Python) together and
  regenerating the goldens. Drift on either side now fails that side's CI.

Validation commands: `cargo test -p gobby-core grant` and
`GOBBY_TEST_PROTECT=1 uv run pytest tests/runtime_grants/test_golden_vectors.py -v`.

**Acceptance:**

- 3.4.1 - A shared golden vector proves both languages reject an unknown-field grant with the typed skew classification. file: `tests/runtime_grants/golden/payload_skew_unknown_field.json`.
- 3.4.2 - Rust pins the grant field inventory against the contract constant. test: `crates/gcore/src/grant/tests.rs`.
- 3.4.3 - Python pins the same inventory against `API_CONTRACT`. test: `tests/runtime_grants/test_golden_vectors.py`.

## P4: Accurate recovery-directive text
`kind: framing`

**Goal**: the code-index rules and skill text describe the CLI that actually ships
after P1–P3 — flags that exist, exit codes that mean what they say, and what an agent
should do when gcode itself reports a typed grant error.

### 4.1 Update code-index rule directive text [category: config] (depends: P1, P2, P3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/code-index/require-code-index-skill.yaml::require-code-index-skill`
- `src/gobby/install/shared/workflows/rules/code-index/require-code-index-skill.yaml::prefer-gcode-for-code-search`
- `src/gobby/install/shared/workflows/rules/code-index/require-code-index-skill.yaml::prefer-gcode-for-source-read`

The three block reasons are the recovery directives agents actually see. Ground them
in the post-P1 surface; conditions and the `gcode_fail_open` machinery stay untouched.
Exact replacement text for each `reason`:

- `require-code-index-skill`:
  `{{ skill_fetch_directive("code-index") }} If that call fails, retry the code search; its recorded failure activates fail-open behavior. After loading, retry with `gcode grep "pattern" [PATH...] -m 50` (supports -F -i -w -l -A/-B/-C -g), `gcode search-content "query" [PATH...]`, `gcode outline path/to/file`, or `gcode symbol <id>`.`
- `prefer-gcode-for-code-search`:
  `Use `gcode grep "pattern" -m 50` (supports -F -i -w -l -g; exit 0 even with no matches) or `gcode search-content "query"` — the code index has full access to this repo and returns ranked, token-cheap results. If gcode errors, read its one-line JSON: a `recovery` field tells you the fix; retry your original command — recorded gcode failures deactivate this rule.`
- `prefer-gcode-for-source-read`:
  `Use `gcode outline <file>` then `gcode symbol <id>` — ranged Read (offset/limit, ≤40 lines) is always available. If gcode errors, read its one-line JSON: a `recovery` field tells you the fix; retry your original command — recorded gcode failures deactivate this rule.`

Template-sync behavior delivers the change: bundled rule rows refresh from templates
on daemon start when definitions drift, preserving enabled toggles. Verification
observes the refreshed installed rows rather than assuming them.

**Acceptance:**

- 4.1.1 - All three block reasons name only flags and behaviors the shipped CLI has, including `-l` and the exit-0-on-empty contract. file: `src/gobby/install/shared/workflows/rules/code-index/require-code-index-skill.yaml`.
- 4.1.2 - Search and read directives tell agents to use the typed error's `recovery` field and fall back via fail-open instead of retrying a failing gcode. behavior: "reason text references the recovery field and fail-open retry" in `src/gobby/install/shared/workflows/rules/code-index/require-code-index-skill.yaml`.

### 4.2 Update code-index skill documentation and doc tests [category: docs] (depends: P1, P2, P3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/code-index/SKILL.md`
- `crates/gcode/assets/SKILL.md`
- `tests/skills/test_code_index_skill.py::*` — scope-reason: doc-pinning tests are updated and added across the module

The shared skill and the gcode-embedded asset are byte-identical bodies (pinned by
`test_code_index_skill_matches_gcode_bundled_asset_when_present`); change both in
lockstep:

- Document the grep compat surface: `-l/--files-with-matches`, the accepted no-ops
  (`-E`, `-n`, `-r`, `-R`), and that unknown flags return a one-line JSON usage error
  with a `recovery` hint.
- Document the exit-code contract in the output-format section: exit 0 always means
  success including empty results (no need to re-verify with a second call); nonzero
  comes with a one-line JSON error on stderr; `--allow-stale` is the only freshness
  bypass (`--no-freshness` does not exist) and is rarely needed now that freshness
  failures degrade to warnings.
- Document typed grant errors: on `payload_skew` or `api_contract_mismatch`, stop
  retrying gcode, report the `recovery` directive to the user, and continue with
  fallback tools (recorded failures fail the redirect rules open).
- Extend `tests/skills/test_code_index_skill.py`: keep
  `test_code_index_skill_documents_allow_stale_flag`, add assertions that the body
  documents `-l`, exit-0-on-empty, and the `payload_skew` guidance, and that
  `--no-freshness` does not appear.

Validation command: `GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_code_index_skill.py -v`.

**Acceptance:**

- 4.2.1 - Both skill copies document the compat flags, exit-code contract, and typed grant-error guidance, and stay byte-identical. file: `src/gobby/install/shared/skills/code-index/SKILL.md`.
- 4.2.2 - The embedded gcode asset carries the same body. file: `crates/gcode/assets/SKILL.md`.
- 4.2.3 - Doc-pinning tests enforce the new sections and the absence of `--no-freshness`. test: `tests/skills/test_code_index_skill.py`.

## E1 End-to-end verification
`kind: verification`

Epic-level acceptance is observed on the installed binaries, not just committed code
(the daemon and hooks shell out to `~/.gobby/bin/`):

1. Rebuild and reinstall receipts: `cargo build --release -p gobby-code` (and the
   gcore-dependent binaries), install via new inode, then probe
   `~/.gobby/bin/gcode --help` shows the grep `-l` flag and
   `~/.gobby/bin/gcode contract` reports `contract_version` 5 with `exit_codes`.
2. Ergonomics probes: `gcode grep -rn "fn main" crates -m 5` succeeds (no-op flags);
   `gcode grep "no_such_token_xyz" -m 5; echo $?` prints exit 0 with empty output;
   `gcode grep needle --type rust` returns one JSON usage line with a `-g` recovery
   hint and exit 2.
3. Skew probe: copy a valid cached grant to a scratch path, inject an unknown
   top-level field, point `GOBBY_MANAGED_EXECUTION_BOOTSTRAP` at it, and observe
   `payload_skew`/`api_contract_mismatch` JSON with the reinstall recovery directive
   and exit 2 — the 08-17 failure shape now self-diagnoses.
4. Directive delivery: restart the daemon, then confirm the installed
   `require-code-index-skill` rule rows carry the updated reason text (DB is the
   source of truth for enforcement).
5. Scoped suites pass: `cargo test -p gobby-code`, `cargo test -p gobby-core`,
   `GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_code_index_skill.py tests/runtime_grants/test_golden_vectors.py -v`,
   plus `cargo clippy -p gobby-code -p gobby-core` and `cargo fmt` checks.

## V1 Plan Changelog
`kind: framing`

- Round 0 — initial narrative draft authored from the local 0.5.0 branch state;
  targets resolved against a fresh code index; live skew evidence (managed bootstrap
  with top-level `credential_generation` rejected by current-source binaries) recorded
  in the Overview.
