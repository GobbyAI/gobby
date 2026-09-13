# Gobby reference-library cutover

Owner: session `gobby#12910`, task `#22275`, epic `#22237`.

## Scope and preservation

The catalog retires exactly 30 bundled skill entrypoints (50 source files), with
no compatibility wrappers. The remaining bundle contains the router, `annotate`,
and 40 reusable standalone skills. Gusto is the 41st retained standalone skill in
this installation: its project-owned row is not part of the distributed bundle.

The native `gcode init` carrier now installs the same `gobby` router as the Python
provider installers. It removes only the byte-identical known predecessor; custom
routers, custom plugin manifests, and other predecessor-directory files survive.
Its retired content remains only in an isolated upgrade test fixture.

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
restart/template operation. Session #13038 is running one bounded Q01 diagnostic;
restart waits for its completion or an explicit interruption-ready notice.
The preserved native artifact allows other sessions to use the shared Cargo
target without changing the binary selected for this cutover.

## Live execution and postconditions

Pending coordinated execution. Record the commit, native new-inode installation,
daemon readiness, provider-carrier refresh, synchronization results, and installed
row/reference/custom-requirement comparisons here before closing #22275.
