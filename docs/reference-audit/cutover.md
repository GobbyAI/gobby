# Gobby reference-library cutover

Owner: session `gobby#12910`, task `#22275`, epic `#22237`.

## Scope and preservation

The catalog retires exactly 30 bundled skill entrypoints (50 source files), with
no compatibility wrappers. The remaining bundle contains the router, `annotate`,
and 40 reusable standalone skills. Gusto is the 41st retained standalone skill in
this installation: its project-owned row is not part of the distributed bundle.

Provider installers own `gobby` router distribution. The native `gcode init`
carrier was retired in #22614. Droid uses `.agents/skills/gobby` as its canonical
carrier and removes `.factory/skills/gobby/SKILL.md` only after successful canonical
installation and byte-verified Gobby ownership. Custom files, symlinks, directories,
and failed migrations survive.

## Isolated verification

All Python database tests use
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
and `GOBBY_TEST_PROTECT=1`. No mutation example was exercised against live state.

- `tests/skills/`: 1,008 passed. This covers catalog/routing, complete and paged
  loads, tracking/reset boundaries, retired-row migration, installation, and
  recorded operating scenarios. Recorded scenarios are replayed fixtures, not
  newly observed model experiments.
- Eight outside-package consumer files: 278 passed initially; two path-wording
  fixtures were corrected and their complete 32-test coverage file passed.
  The files are `tests/agents/test_merge_orchestrator_contract.py`,
  `tests/dispatch/test_rules_stage_native.py`,
  `tests/docs/test_expand_skill_contract_section.py`, `tests/plans/test_covers.py`,
  `tests/plans/test_plans_parser.py`, `tests/sessions/test_handoff.py`,
  `tests/storage/test_storage_skills.py`, and
  `tests/workflows/test_monolith_guard.py`.
- `tests/install/test_bundled_content_manifest.py`: passed real sdist-to-wheel
  packaging and exact generated-manifest membership/hash comparison.
- `cargo nextest run -p gobby-code -E 'test(skill::tests)'`: 8 passed.
- `cargo clippy -p gobby-code -- -D warnings`: passed.
- `cargo build --release -p gobby-code`: passed. The preserved release artifact
  at `/tmp/gobby-reference-cutover-gcode-12910` has SHA-256
  `b646c9eabfa98f3b7ef5a9627f1a7a7238c527db666f79edbe3f52c75e33aedb`.
- Touched-test quality audit: 36 files, 301 tests, zero issues. Touched-test type
  audit: 36 files, zero errors after explicit optional-row and fixture narrowing.
- Python suppression ratchet: 4,307 files, 217 baseline suppressions, zero new
  or stale entries. No baseline relaxation or suppression was added.

The final scoped rerun after the last formatting/scenario edits passed 134 tests
across reference-library coverage, scenario replay, installation, stage consumers,
planning rejection/authority, and coverage-path handling. The final five-file
type and quality reruns report zero errors/issues. The full repository pytest
suite was not run.

## Documentation and review corrections

Planning review restored both-route elicitation, task-ready atomic handoff,
mechanical-repair prerequisites, closed enhancement values/full presentation,
and class-specific lesson ranking. Capture pagination, active merge-resolution
continuation, generic commit examples, and supported handoff instructions remain
explicit in their operation references. Linked guides and the coverage contract
were corrected with them.

Historical tests now read the relevant references or their authoritative linked
contracts. Real review-evidence recovery tests retain service calls and isolated
index state. Malformed-plan tests pin the intended diagnostic. Scenario acceptance
also checks operation-reference availability before each action, so loading only
the router/overview cannot masquerade as loading the old monolithic skill.

## Live preflight — 2026-09-13

Before cutover, the fully paginated `list_skills(include_internal=true, limit=500)`
result contains 73 rows. All 30 predecessor names remain installed. Gusto is
project-owned, enabled, ID `94c31a55-04d9-58f1-bce5-20d6524db70c`; the router ID is
`988f1981-fd8a-531e-a114-7873f1e35ea3`. The new admin daemon reference is not yet
available in the installed router, confirming that source authoring alone has
not published the library.

`uv run gobby status` reports development mode, Python daemon PID 70097, HTTP
60887/WebSocket 60888, healthy datastores, and matching checkout/installed/live
schema v435. The cutover does not change schema or datastore topology.

Project coordination broadcasts `2398a709-6b03-4fa3-aca7-a2e3b807e929` and
`376df98f-c4fb-41ec-ac7a-b50407e27cd3` announce the retirement and request a quiet
restart window. Sessions #13086, #13088, #13107, and #13114 report no competing
restart/template operation. Session #13038 completed its bounded Q01 diagnostic
and committed its Ask fix before giving the restart-ready notice.
The preserved native artifact allows other sessions to use the shared Cargo
target without changing the binary selected for this cutover.

## Live execution and postconditions

Completed on 2026-09-13. Main cutover commit: `9acef616df`; provider preservation
fix: `1a5bd37600`; sync diagnostic reporting fix: `2e6885d`.

Session #12967 explicitly cleared the window after the two-minute heads-up.
Broadcast `2f640ba2-7429-4eec-b971-e034342c3ad2` announced its start. The normal
`promote_workspace_binary_set` helper verified the candidates' embedded identities,
acquired all three binary locks, signed staged files, promoted through new inodes,
and wrote the coherent identity stamp. Installed signed SHA-256 values:

| Binary | SHA-256 | New inode |
| --- | --- | --- |
| gcode | `4174abf7f6f65b88cb91bf3a1617edd23f5f78479450f2852cdd61dfa8c9824d` | 404951332 |
| gdaemon | `f01d0c8eb2c21ba367a150d0756bc91c239ba9a09f0de5786b85980b2bd5a6ba` | 404951339 |
| ghook | `a991612190e94b05bfae793e992b36dfa77fe418570dcb30d152f1302a29c3b4` | 404951342 |

Signing explains the difference from the preserved build hashes. `gterm` was
excluded at its active owner's request: its hash, inode 402421574, and host PID
49820 remained unchanged. Session #12967 independently verified all hashes,
inodes, and schema identity, and announced that its workers reconnected normally.

`uv run gobby restart` exited zero: service-managed daemon PID 32845 passed
health checks, with MCP, code index, embeddings, vectors, communications, sessions,
agents, cron, and pipeline recovery initialized. `uv run gobby status` confirmed
healthy datastores and checkout/installed/live schema v435. The exact assets root
remains `fc40c70e4a6b2349bbcfc961333f5defb99f7f1394a9c4e3d549da62dfc0fe14`.
Session #13038 received its keyed runtime-ready release for direct Ask validation.

The checked Python carrier installers refreshed seven existing carrier files:
global Claude commands/skills, Codex skills, shared agent skills, Qwen skills,
and project Claude/shared agent skills. All seven were verified historical bundled
versions; each now hashes to
`358c8f3e7201905e6a30521c50b2f355e498a466b8b74661b21939d386b8f32d`.
No custom carrier was overwritten. Exact historical generated carriers are also
recognized; alias removal follows successful replacement. Custom files, symlinks,
extra files, empty directories, and prior backups are covered by isolated tests.
The obsolete prefix-based backup sweep was removed.

Post-cutover `list_skills(include_internal=true, limit=500)` returned 43 rows.
The complete 22,308-character oversized result was consumed through all four
`get_tool_result` slices using returned offsets. Exactly the 30 catalog-folded
names disappeared; 42 bundled entries and private Gusto remain. Gusto retains its
ID, project ownership, enabled state, and content SHA-256
`686ae42bd1bffa96b5bf86d20dc60251a83ebc03081b21bf7cb02c8b983e381c`.
Read-only storage comparison verified all 165 router files against source,
including the catalog and 164 references, without missing or extra files.

Installed `bootstrap-default-agent-core-skills` remains enabled and now loads
`gobby:references/skills/loading.md`, `gobby:references/memory/overview.md`,
`brevity`, and `restraint`. Enabled `require-code-index-skill` requires the exact
code-index overview. Sixteen Gobby-owned persisted requirement fields contain
24 exact references and zero retired names. A real edit after loading only the
router was blocked by the development gate; loading
`gobby:references/development/obligations.md` allowed the same edit. A complete
task-closing reference load appeared independently in `loaded_skill_references`.

`uv run gobby sync --verbose` exited zero with no changes after startup sync.
The reporting fix exposes returned warnings in the CLI and logs, and returned
failures produce a nonzero CLI exit. Final sync reported 86 preserved user/runtime
requirements: 18 fields in enabled agent workflow instances and 68 task fields.
These are not template-owned and were not rewritten. Exact locations and
replacements are in the machine-local report
`~/.gobby/reports/gobby-reference-library-12910.txt`; rerunning normal sync
regenerates the diagnostics. Broadcast `ffe5bc41-d92f-4ea9-9ca7-1531ebe31bd8`
notified active owners. Historical records need not be rewritten.

Follow-up verification: 80 focused carrier/Claude/Qwen/routing/installation tests
passed; 40 focused sync fan-out/CLI/reinstall tests and 78 shared-installer tests
passed. Ruff, production mypy,
touched-test type/quality audits, and the suppression ratchet passed. Delegated
reviews covered all nine preservation paths and all four diagnostic paths with
no open findings. No full repository pytest run was used.

Final acceptance rerun after both follow-up fixes:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_reference_installation.py tests/skills/test_reference_library.py -q --no-cov`
passed all 24 tests in 50.61 seconds.
