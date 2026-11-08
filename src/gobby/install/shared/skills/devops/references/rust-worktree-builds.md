# Rust Builds Across Many Worktrees

Many agents building one Rust workspace in separate worktrees multiply build time, disk
and file churn. Measure where the cost goes, then cut it at the source.

## Where Builds Go

Check the effective settings before measuring:

```bash
for v in CARGO_TARGET_DIR CARGO_INCREMENTAL CARGO_BUILD_JOBS RUSTC_WRAPPER; do printf '%s=%s\n' "$v" "$(printenv "$v")"; done  # named, nonsecret only; never dump env
cargo metadata --format-version 1 --no-deps | jq -r .target_directory
```

Gobby launches agents with their own build roots under `~/.gobby/cache/` (sandboxed and
unsandboxed trees differ) unless the project configures another root. The host profile
records the current roots.

Agent builds are plain incremental builds. Gobby sets `CARGO_BUILD_JOBS=4` and removes
`CARGO_INCREMENTAL`, `RUSTC_WRAPPER`, `SCCACHE_DIR` and `SCCACHE_SERVER_UDS` when it
starts an agent CLI. Cargo treats any set `CARGO_INCREMENTAL`, blank included, as
incremental off, so a value in a shell profile or daemon environment would slow every
edit loop.

## What Costs The Most

| Cost | How to see it | Lever |
|---|---|---|
| Rebuilding dependencies per tree | Clean build time of a new tree | Keep each tree's build root between builds; a compiler cache does not help across checkouts (sccache measured 0 of 416 cross-checkout hits) |
| Debug info size | `du -sh <root>/debug/deps` | `[profile.dev] debug = "line-tables-only"`; panics keep file and line |
| Incremental state | `du -sh <root>/debug/incremental` | Keep it: it makes the edit loop fast; it goes when its build root goes |
| Many test programs | `ls <root>/debug/deps \| grep -c -v '\.d$'` | One integration-test program per crate (nextest isolates each test in its own process) |
| Stale executables | Old hashes in `deps/` | Prune executables no current build references |
| Orphaned build roots | Roots whose worktree is gone | Sweep after verifying the tree no longer exists |

Measure each before and after: folder sizes, executable counts, clean build time,
rebuild time after a one-line edit, and the security-service and file-watcher CPU during
the build.

## Shared Target Directory: When It Fits

One `CARGO_TARGET_DIR` for every worktree compiles dependencies once and keeps disk near
one copy. Cargo's build lock serializes concurrent builds, which also caps load.

It breaks when branches change a crate other crates depend on: a tree can read a
peer's freshly built library and fail with "no method named X", or pass against the
wrong version. For gate runs on such branches, give each tree its own target directory;
each tree then builds its dependencies once, and incremental builds keep later builds
cheap.

On an agent fleet the lock serialization becomes a queue, so prefer per-tree roots with
incremental builds, and keep shared targets for a few leaf-only branches.

## Build Volume (macOS)

A dedicated APFS volume for build output keeps churn away from Spotlight, fseventsd
logging and Time Machine. Setup is operator work (root), and the host profile records
whether it exists:

```bash
diskutil apfs addVolume <container> APFS <Name>   # container id from diskutil list
sudo mdutil -i off /Volumes/<Name>
sudo touch /Volumes/<Name>/.fseventsd/no_log      # the folder already exists
sudo tmutil addexclusion -v /Volumes/<Name>       # terminal needs Full Disk Access
sudo chown "$USER":staff /Volumes/<Name>
```

Recommend it only when measurement shows file-watcher or indexer CPU during builds.

## Safe Cleanup

- Never delete a build root whose worktree still exists or whose build is running
  (`pgrep -fl cargo`, `lsof +D <root>` on a small root).
- `cargo clean` inside a live tree costs the next build a full rebuild; prefer pruning
  stale executables.
- Every deletion needs the operator's approval; report sizes first.

_Version: 1.1.0_
_Last verified: 2026-11-07_
