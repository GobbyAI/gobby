# Legacy wiki retirement receipt

The removal, retained code-index cleanup and runtime cutover are complete on
`0.5.0`. This final execution receipt supersedes the earlier checkpoint history;
the approved plan and linked task history remain accessible.

The user retained the Git archive and explicitly waived preservation, backup,
recovery and further rehearsal of wiki data and generated files. Canonical session
records, original sources, memories, project records, unrelated code-index state
and explicit grants remain protected. Replacement planning is separate work.

## Source and runtime

- Epic: #21771; integration: #21840; sessions: #11777, #11967 and #11969.
- Removal worktree: `wt-epic-21771`, based on `0.5.0` at
  `53e862d53d40027173ba2d74d86858d44fbc88f4`.
- Annotated Git tag: `legacy-wiki-before-retirement-21771`, verified at that base.
- Managed removal merge: `3e5662c996eaa325e7e632b180aa781d6b6e28a4`.
- Main cutover commit: `f772e7b1a16cc83dfd45a98d0e73c1b74b4a4cd9`.
- Native retirement fixes: `b520be379071ffe7ac5cada46d1fe2923e30fe08`
  and `0db8bf5ea11fe52e33efa5f79756a711afda057c`; managed landings
  `f62d578c8a7d50b9f4237f98caa6adb7b06e633a` and
  `a8b82e9d968a822382c6a157fe3e663fd466cdb4`.
- Main daemon restored at schema 426; installed/source/live schema identities agree.
- All five surviving native binaries rebuilt and installed through the canonical
  promotion path using new inodes. `gterm` includes its `vt-engine` feature.

The wiki crate, Python services, MCP registration, HTTP routes, CLI/install wiring,
configuration, UI/settings/navigation, writers and session-summary mirrors are
removed. Terminal redaction, attachment forwarding and Mermaid utilities now live
with their surviving consumers. Canonical summary generation, revisions, handoffs
and transcript processing continue independently.

## Data deletion

The immutable direct inventory is
`~/.gobby/retirement/f7b3c8517e45fd4ddba044d64d4cb78cabc3d276ac6d28bc57c213c46141d897/inventory.json`.
It identifies 21 filesystem roots, 28 datastore/registry targets and 11,343 exact
code-index paths across three projects. The initial direct apply completed 36,217
items with no errors before deferred schema and code-index work.

All 21 wiki roots are absent, including both Gobby-owned installed `gwiki` binaries.
Imported copies, generated pages, vaults, manifests, exports, mirrors and discovery
registrations were deleted within the inventory's ownership boundaries. Original
files outside those roots were preserved. A final check also removed 25 stale
wiki bytecode files; `src/gobby/wiki` and `crates/gwiki` are absent. Evidence is
`~/.gobby/retirement/21840-wiki-bytecode-cleanup.json`. The five dedicated PostgreSQL tables,
five wiki Qdrant collections and exact `gobby_wiki` graph are absent; wiki settings,
discovery rows, rule, variable and 17 jobs are removed.

Canonical maintenance epoch `c34ececc-ffc7-4a19-af31-23ac638190c0` completed migration
425→426. Its existing shared-hub backup passed all five archive/restore verifiers;
this is the platform migration safeguard, not a wiki recovery requirement. Two
focused fixes (`e69da8aaeb`, `61254d8e98`) allow older incompatible backup manifests
to remain while fully validating the newest selected backup.

Two inventory-bound graph addenda removed 102,666 exact edges and 25,604 selected
nodes across Gobby and Gobby Web. Complete selected-path absence checks passed;
the final pass preserved all 3,555 other endpoints. Receipts beneath the inventory
root are `legacy-unversioned-graph-apply.json` and `remaining-gcode-graph-apply.json`.
A separate exact-ID cleanup removed 6,394 unlisted historical vectors, verifying
all 8,362 native-listed full points unchanged before native retirement. Its receipt
is `~/.gobby/retirement/21840-qdrant-extras-cleanup-20260905T231958218338Z.json`.

The original inventory is unchanged. An endpoint-supersession receipt records the
corrected canonical Qdrant URL with identical physical backend identity. Earlier
refusals and partial results remain recorded. Native preflight performance was
corrected in `b520be3790`; receipt reads and writes were buffered in `0db8bf5ea1`
while retaining explicit flush, file sync, atomic persist and directory sync.
The rebuilt `gcode` was promoted through a new signed inode. New promotion evidence
is `~/.gobby/retirement/21840-gcode-promotion-20260906T010904Z.json`; the earlier
`21840-final-gcode-promotion.json` receipt remains unchanged.

All three immutable native receipts are complete with zero errors:

- Gobby: 6,798 paths and 6,781 versions absent.
- Gobby Web: 4,067 paths and 4,041 versions absent.
- Retirement worktree overlay: 478 paths and 483 versions absent.

The receipts are `<project-id>.native.json` under the inventory root's
`code-index-apply-receipts/` directory. A whole-inventory retry stopped before
native mutation after an unrelated inventoried Game Goblins worktree had been
removed. The immutable native manifests then completed directly with their source
digest, project, machine, backend, symlink, ownership and reappearance guards intact.
No blanket database, vector or graph reset was used.

## Preservation and validation

`~/.gobby/retirement/21840-canonical-verify-20260905T222846455895Z.json`
verified zero missing or changed records among 3,780 memories, seven projects,
313 handoffs, 11,743 summary revisions and 9,900 canonical session records. Reserved
grants were unchanged. Thirty reviewed wiki-guidance memory corrections occurred afterward under
#21772, preserving memory identity and metadata.

Focused checks passed; the full pytest suite was not run:

- Session preservation, forwarding, redaction, shutdown/lifecycle and schema tests,
  including the final 131-test retirement run and 160-test lifecycle run.
- Final native retirement/graph/GC run: 69 passed in 36.800 seconds, including
  interrupted/completed retry, recreated projections, null identities, late-batch
  unknown edges and invalid records beyond 10,000 results. Final backup-selector
  tests: six Rust and 22 Python passed.
- Final receipt-I/O regression run: 14 native `retire_files` tests passed in
  26.052 seconds with all ignored endpoint fixtures enabled. `cargo fmt`, full
  gobby-code Clippy with warnings denied and the release build passed. The focused
  direct-retirement Python check passed 11 tests in 2.54 seconds.
- Full `src/` Ruff, format and mypy checks; scoped script checks, Rust Clippy,
  release builds, schema-contract checks and Actionlint on changed workflows.
- Explicit whole-file deletion is exempt from plan size lint (`b372908c3d`);
  regression coverage retains the size safeguards for edits.
- Frontend production build and 33 focused navigation/settings/shortcut tests.
- Installed-binary isolated daemon test passed in 33.66 seconds: fresh sessions
  receive no wiki injection, restarts do not regenerate content, original files
  and prior session data survive.
- Live health returned 200; five retired wiki endpoints matched the ordinary
  missing-route 404 response. MCP discovery exposed 26 servers and 434 tools with
  no wiki server or tools. PostgreSQL, Qdrant and FalkorDB remained healthy.
- Live `gcode` symbol search found `SessionManager`; its surviving module graph
  returned 85 nodes and 26 edges. The private graph receipt is
  `~/.gobby/retirement/21840-gcode-surviving-graph.json`.

Task records contain the exact commands and commit-linked validation evidence.
The static test-quality auditor flags three explicit fixture-only Rust ignores;
all three executed successfully through `--run-ignored all`. Their endpoint
safety guards and the audit baseline were preserved.

## Registry and cleanup

All 38 user-listed obsolete tasks are closed as `obsolete` and unclaimed.
The `wiki-output-design` plan was archived through its registry at 23:00:13 UTC
on 2026-09-05, preserving its content hash. The byte-identical archived plan and
retired coverage record are committed as `ea82ce1e49`. Shared #21577 and #21586
remain open with only their gcode obligations. Historical task records, linked
commits and explicit grants remain intact.

The earlier wiki recovery copies were discarded: 67,614 owned entries and seven
rehearsal containers/volumes. Private receipts are
`~/.gobby/retirement/21771-discard-recovery-data.json` and
`~/.gobby/retirement/21771-discard-rehearsals.json`. Three isolated native-test
containers recreated for final validation and their exclusive PostgreSQL volume
were removed by exact recorded IDs. All five unrelated container IDs remain
running. Final evidence is
`~/.gobby/retirement/21840-fixture-cleanup-20260906T013647Z.json`.

This receipt is landed through the managed worktree service. The worktree is then
deleted with the default safe flags; task #21840 records that final lifecycle event.
