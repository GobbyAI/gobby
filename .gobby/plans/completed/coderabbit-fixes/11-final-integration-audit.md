# CodeRabbit `0.5.0` Forward-Port Integration Audit

## Scope and refs

- Safety checkpoint and unchanged live `origin/0.5.0`:
  `bf1b1bd2184adf62e5674fd7eaf6737cef6e87bd`
- Integration branch: `coderabbit-forward-port-18998`
- Preserved source branch: `coderabbit-fixes-18998`
- Source range: `0.4.94..coderabbit-fixes-18998`
- Integration range: `0.5.0..coderabbit-forward-port-18998`
- Unique source paths: **239**
- Paths also changed by current `0.5.0`: **44**, including **29** production paths

## Ordered packet correspondence

| Packet | Source | Integration | Original | Integration task |
| --- | --- | --- | --- | --- |
| Tasks, plans, review learning | `7daf01cf5552fc65d2e07ffaca06016ac7db6fa5` | `da44e2fa9e42fc149a7414c1b1492dda48af6269` | `#19004` | `#19170` |
| Workflows, skills, orchestration | `f8dbf6d9291d5071636da6f233ceca75fecbd829` | `1242097f2fb0abd571b70c4339b462b9e4131390` | `#19005` | `#19171` |
| MCP proxy and Hub services | `6d121d19bf8b8111727d503af2ea00d09dd85430` | `9c34494c6ce5d1c6f8688fea7c042804907b3136` | `#19006` | `#19172` |
| Servers, sessions, runtime | `e95f9be14bcd1d821de8aff8efdea73fdf3536ec` | `c3c13e0ed4af73e6dc103322c559f16ffa5e5bd5` | `#19007` | `#19173` |
| Runner, dispatch, storage | `479f36f3719b3e056fcd067a6494bdc5ea495303` | `086d75a1da59646694d2fa6e3c6aee31a0105ac6` | `#19008` | `#19174` |

`git range-diff --no-patch` maps all five source commits to the ordered integration
commits. The integration range also contains the independently task-linked corrective
commit `23e5b2d74cb5f867a2a575e388a8cb96736f35db` (`#19177`), which keeps memory
recall redirects action-framed, plus this final integration audit commit.

## Finding disposition audit

Every finding has one explicit `Carried`, `Adapted`, `Already satisfied`, or `Obsolete`
disposition in its packet ledger:

| Ledger | Findings |
| --- | ---: |
| `06-tasks-plans-review-learning.md` | 59 |
| `07-workflows-skills-orchestration.md` | 55 |
| `08-mcp-proxy-hub-services.md` | 53 |
| `09-servers-sessions-runtime.md` | 53 |
| `10-runner-dispatch-storage-core.md` | 57 |

The ledgers also record the reviewed overlap paths and the current contracts retained
or adapted during conflict resolution. No whole-file `ours`/`theirs` resolution or
backward-compatibility shim was used.

## Migration and source-size audit

- Migration `339_expired_plan_review_round_retry.sql` was removed because migration
  `338` already creates the same interactive-round and stage-round unique indexes with
  the same columns and `expired_at IS NULL` predicates.
- Migration `342_task_validation_epoch.sql` repairs provisional outcomes before adding
  its normalized-outcome constraint `NOT VALID`, then validates it separately.
- Current migrations `345` and `346` are unchanged.
- Every affected non-test Python, TypeScript, TSX, and CSS source file remains below
  1,000 lines.

## Final validation

Required global gates:

```text
uv run ruff format --check src/ tests/
  3372 files already formatted
uv run ruff check src/ tests/
  All checks passed
uv run mypy src/
  Success: no issues found in 1665 source files
uv run gobby test-types audit tests/ \
  --baseline .gobby/test-types-baseline.json --fail-on-new
  1706 files scanned; 0 new errors
```

The final test manifest was built from:

- 120 test files changed by the five source commits;
- 84 test files changed by the 27 current `0.5.0` commits that touch the 29 overlapping
  production paths;
- two final-integration test paths found by the global format gate.

After deduplication and removal of one obsolete source test path, the manifest contained
191 files and 5,052 tests. The definitive final run was:

```text
GOBBY_TEST_PROTECT=1 uv run pytest <191-file union> -q
  5051 passed, 1 skipped
```

The first union run exposed two test-boundary defects. The liveness test now pins the
configured tmux command it asserts instead of depending on daemon-wide test order, and
the model-metadata fake accepts psycopg `Composable` timeout statements. The four
final-integration test paths pass 102 tests with zero medium-or-higher test-quality
issues and zero new test-type errors.

Final correspondence and whitespace commands:

```text
git range-diff --no-patch \
  0.4.94..coderabbit-fixes-18998 \
  0.5.0..coderabbit-forward-port-18998
git diff --check 0.5.0..coderabbit-forward-port-18998
```

The source branch and its worktree remain unchanged at
`479f36f3719b3e056fcd067a6494bdc5ea495303`.
