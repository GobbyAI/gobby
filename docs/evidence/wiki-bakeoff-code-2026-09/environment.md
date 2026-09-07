# Code-wiki bakeoff environment

Provisioning and live acceptance checks passed on **2026-09-07** for task **#21942** under epic
#21926. All six comparators and pinned gcode are installed. No wiki generation has started;
provider compatibility, embeddings, cold baselines, and presentation scoring belong to subsequent
leaves of the approved [plan](../../../.gobby/plans/wiki-code-bakeoff.md).

## Ownership and input boundary

Runtime: `/Users/josh/Projects/wiki-bakeoff-code-2026-09`, mode **0700**, owner task **#21942**,
initial owner session **#12034**. Continuation session #12124 retains that ownership; helper
commands still use `--owner-session '#12034'`. All paths below are relative to this runtime.
Tracked code and evidence live only in `docs/evidence/wiki-bakeoff-code-2026-09`.

The [frozen matrix](matrix.md) defines 69 independent corpus copies. All manifests passed actual
byte/hash, exclusion, symlink, and secret-pattern checks. Baseline commit:
`0216f1e33f05962d49467d95fe84609041c6dba8`; change commit:
`8b24ac26699aac8b24254a647aa70b208287b492`. Generated outputs, untracked files, runtime state,
`.gobby/project.json`, and `.gobby/mcp/servers/lightspeed.yaml` are excluded from inputs. C0
installation probes do not count as C1 cold-generation evidence.

`isolation/external-state.before.json` and `isolation/external-state.after.json` capture the source
checkout, stable global files, existing Docker containers and volumes, and shared listeners.
`isolation/concurrent-changes.json` records narrowly verified concurrent activity:

- Game Goblins coordinator `5871e9f4-4773-454d-8239-f6519068d6f0` attributed the source advance
  `8463aec599de552dc18dcc74c1c01a4ffd54b42d` to
  `b291c065f5ad5e06d1294cc9619b7d76557836bf` to its task #158–#161 merges through
  `merge_worktree`, followed by pushes on owner instruction. Git ancestry and full first-parent
  commit identities are retained in `isolation/concurrent-source-history.log`. The source's
  pre-existing untracked status is byte-identical before and after.
- The sole global configuration difference is Codex's trusted worktree path changing from
  `task-158-fill-the-conway-floor-to-its-rack-on-the` to
  `task-162-persist-daily-run-positions-and-record-d`, both under
  `/Users/josh/.gobby/worktrees/game-goblins/`. Replacing that single path in the captured after
  bytes reconstructs the exact before SHA-256
  `1283cb2a99f68a737a64c67e44e212bd0e8e9000694fc40c4a7b6a6b611d1e65`.
  The after SHA-256 is `728589471da81d18ccfe1d008b0da52c0d4eb5d3ec08e03c208001c2ecf69352`.
  No other bytes changed. The private capture is 0600 and remains outside Git.
  Its mtime, `17:54:24.020368 UTC`, aligns with the coordinator's confirmed task #162 spawn at
  `17:54:24.401672 UTC`. Gobby's native `_seed_codex_trust` prunes deleted worktree trust and
  adds the new worktree. The user reported no known manual settings change.
- All other captured stable global files match. Existing container identities, images, mounts,
  networks, labels, bindings, and health match; only elapsed-time display fields differ.
  Shared listeners match exactly. Added containers and named volumes are exactly the owned
  bakeoff services below. The validator rejects additional changes.

An early private-daemon probe exposed Qwen discovery writing two owned-directory trust entries
into global Qwen stores. The daemon was stopped, exactly those entries were removed, and all
unrelated entries were preserved (`receipts/qwen-discovery-cleanup-12124.json`). Its corrected
PATH contains only the private Python bin directory, `/usr/bin`, and `/bin`; provider CLIs are
verified absent before launch. Final stable-file hashes match. No global sandbox permissions,
shared binary installation, or comparator application installation was changed for provisioning.
The separate Gobby hook fixes described below required an announced main-daemon restart.

## Verified installations

`receipts/installations.json` and `receipts/gcode.json` contain executable artifacts, dependency
locks, source/archive receipts, manifests, command argv, working directories, exit codes, and log
size/hash records. `record_installations.py` ran the native checks; the validator rehashes the actual
contained regular files and every original source file. All pinned source manifests match.

| Tool | Frozen identity | Native validation and dependency evidence |
| --- | --- | --- |
| Graphify | `c9f99018774e2e0380e9f65b3959944559a0d5f6`, 0.9.55 | Private Python 3.13.15 environment; frozen uv lock; version and dependency check passed. |
| Understand Anything | `07edf82a04371b6f69779b067bdc8a1a8753a9db` | Private pnpm 10.6.2; frozen workspace lock; core build/import and 116 native parser tests passed. |
| Archify | `c6519401f7b91b9d43011657880893b0a8955548` | Locked npm dependencies; native `generate-validators.mjs --check` passed. |
| CodeWiki | `2584854d7538dc3e3e8e6839cf8590b0cd12a431`, CLI 1.0.1 | Private Python 3.12.14 editable installation; pinned requirements receipt; version and dependency check passed. |
| OpenDeepWiki | `75840e5e86213ca40ace9d5036b1f52603f8d038` | Pinned .NET SDK image; NuGet locks; frozen `bun.lock`; private Bun 1.4.2 build; native backend and web health passed. |
| Grok Wiki | Release 0.0.38 | Signed DMG SHA-256 `bbcccef258102a1f32e36947b1a6da059a828bf1cea552dfdb58ca35ab562e09`; embedded dependencies; deep/strict signature, bundled CLI help, and native health passed. |
| gcode / gdaemon | Gobby `7394b97c1d88c82f685e788e798de2cfd728ad15`, gcode 1.7.0, contract 8 | Private Cargo.lock/release builds; native version, contract, BM25 integrity, graph rebuild, and Qdrant projection query passed. |

Pinned gcode SHA-256:
`1f64d7400001a890ab6630ea7a823d8dbaf7e6b18513d3bb3bed6a07075f7652`.
Pinned gdaemon SHA-256:
`d34e0bf08e70e2d4f38076f03950348e711a2567e6e56ff52437551d8b40e0b4`.
The shared binaries were not replaced. An initial CPython 3.14 `msgspec==0.20.0` import hang was
resolved by rebuilding that exact locked version, ad-hoc signing the extension, and installing
through a new inode. Final extension hash:
`a1735e97d8d9e32ab2f5f65b3d0dfe8ace50d61d858e4c8b66872b133cc479ca`.
Failed samples and successful build/import logs remain under `logs/install/msgspec-*12124*`.

Archify and Understand skills are activated only in comparator-local
`workspaces/<comparator>/.agents/skills`; targets and entrypoint hashes are recorded in
`receipts/project-local-skills.json`. This follows Codex's
[documented repository-local discovery](https://learn.chatgpt.com/docs/build-skills).
For Understand, set `CLAUDE_PLUGIN_ROOT` to
`<runtime>/sources/understand-anything/understand-anything-plugin`. Keep auto-update absent or
use `--no-auto-update`; frozen skills are unchanged. A managed read-only Sol/xhigh worker verified
both skill trees and hashes; generator discovery is still a compatibility check.

## Native services and signed grants

All bindings are loopback. Docker project `wiki-bakeoff-code-2026-09` owns network
`wiki-bakeoff-code-2026-09-network` and the three `<project>-{postgres,qdrant,falkordb}-data`
volumes. Image digests and rendered configuration are checked before service creation; live
inspection verifies identities, mounts, privileges, networks, and exact published ports.

| Service | Binding | Recorded owned identity |
| --- | --- | --- |
| PostgreSQL | 61234 | Container `23a820ce66eb5c4a34ed8e56ba648be4e77842b1a070e3520f04ec9d98c35c16` |
| Qdrant | 61235 HTTP, 61236 gRPC | Container `d506a8f2b974282d9d54c4340a7ed47e0b7fb68fdde0053cc614ae0a35b66393` |
| FalkorDB | 61237 Redis, 61238 browser | Container `e38c1edcd6edc10217d51211ab8cd7ec4ba4d53d6e025f10e265d3f1161c0b4e` |
| Isolated Gobby daemon | 59480 HTTP, 59481 WebSocket | PID 4643; `receipts/daemon-launch-12124-public.json` |
| OpenDeepWiki backend | 61239 | Container `3b4877d177bd9fa2ce2a6caebda9c5c05e2255286bfd77b145ce88ef9051572d` |
| OpenDeepWiki web | 61240 | PID 89480; `receipts/opendeepwiki-web-launch.json` |
| Grok Wiki native CLI server | 61241 | PID 56701; `receipts/grok-wiki-launch-12124b.json` |

`receipts/active-launches.json` binds the active launch receipts by actual hashes.
`receipts/services.json` and `receipts/ownership-live.json` contain service/image/network identities.
The .NET SDK image is
`mcr.microsoft.com/dotnet/sdk@sha256:4beef5b8919dcaa2dc924233bd069257e883cc7a061e09088a97d152d6a48510`.
Complete backend build/run argv are in `receipts/opendeepwiki-{build-command,launch}.json`.

The isolated daemon uses `gobby-home/bootstrap.yaml` and `gobby-home/files`, private credentials,
and independently seeded account/machine identity. tmux, terminal hosting, cron, memory dreams,
task expansion/validation, and automatic code indexing are disabled. Config revision remains **4**
after two preparation reruns. Preparation receipts are revision-specific and immutable.
The image's `_pgaudit_probe` is preserved outside native `public`, in `bakeoff_image_audit`;
the original OID 17298 and one row were preserved. Legacy private schema/failed attempts remain
for evidence. A dedicated temporary database regression verified relocation, repeat safety,
and ambiguous-target rejection, then removed only that temporary database.

C0 project **`263a50fd-d414-4dbc-aad4-3c427bf49254`** was initialized through the pinned native CLI.
That installation probe indexed 142 files / 2,499 symbols; both BM25 indexes report healthy.
Native graph rebuild synced 142/142 files, and native vector cleanup queried the isolated Qdrant
projection with zero vectors. Raw commands/results are `logs/install/gcode-*-public-12124.json`.

`validate_environment.py --live` refreshes the normal native grant cache through pinned gcode,
checks native status `id` and `root_path`, and validates grant version 2 / grant API contract 1
(separate from gcode contract 8), current expiry, project/machine binding, private PostgreSQL
role/database/public schema, Qdrant URL, and FalkorDB endpoint. It presents the signed grant to
`/api/runtime/config`: **200**; a changed signature returns **403 `invalid_signature`**.
It connects with the granted PostgreSQL credentials and verifies both required extensions,
queries Qdrant collections, and authenticates FalkorDB PING. No DSN or grant secret is printed.
`receipts/runtime-verification.json` retains the sanitized observation; live validation performs
fresh checks rather than relying on historical booleans. Grants expire and refresh normally.

OpenDeepWiki has isolated SQLite/state under `state/opendeepwiki/service`, English output,
concurrency one, disabled scheduled updates and Slack integration. The backend mounts only owned
publish, state, and corpus paths; publish and corpus mounts are read-only. No repository or optional
Graphify job has been submitted. Backend `/health` and native web `/` both return 200.

Grok Wiki runs the signed app's bundled Bun/server CLI using documented
[`GROK_WIKI_ROOT`](https://github.com/AsyncFuncAI/grok-wiki/blob/0.0.38/skills/grok-wiki-cli/SKILL.md)
set to `state/grok-wiki`, telemetry off, and native generation capacity one. `/api/health` confirms
private file storage, no configured external database, empty queue, disabled secret grants, and
local-cli access without an API key. The native CLI server's `/` is **404** because this bundle
does not serve the desktop UI assets. Installation and service checks pass; **web presentation is
unvalidated** and must be exercised through native surfaces later. No desktop app was launched.
Pinned docs, CLI help, and health responses are retained under `logs/install/grok-wiki-*`.

## Reproduction, checks, and teardown

Do not rerun initialization or `launch_services.py start` in the existing runtime. The original
initializer, image/build commands, dependency locks, failed attempts, and successful receipts are
retained. Existing-state checks are repeatable:

```bash
GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1 uv run python docs/evidence/wiki-bakeoff-code-2026-09/launch_services.py observe --owner-session '#12034'
uv run python docs/evidence/wiki-bakeoff-code-2026-09/record_installations.py --owner-session '#12034' --attempt 12124
uv run python docs/evidence/wiki-bakeoff-code-2026-09/validate_environment.py --live
DATABASE_URL='postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test' GOBBY_TEST_PROTECT=1 uv run pytest docs/evidence/wiki-bakeoff-code-2026-09 -q
uv run ruff check docs/evidence/wiki-bakeoff-code-2026-09
uv run ruff format --check docs/evidence/wiki-bakeoff-code-2026-09
```

Results: live validation passed; **55 focused tests passed**. Touched-test quality audit passed
with zero findings at severity low; touched-test type audit passed with zero errors. Exact audits:

```bash
uv run gobby test-quality audit docs/evidence/wiki-bakeoff-code-2026-09/test_daemon_preparation.py docs/evidence/wiki-bakeoff-code-2026-09/test_environment_validation.py docs/evidence/wiki-bakeoff-code-2026-09/test_runtime_boundary.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
uv run gobby test-types audit docs/evidence/wiki-bakeoff-code-2026-09/test_daemon_preparation.py docs/evidence/wiki-bakeoff-code-2026-09/test_environment_validation.py docs/evidence/wiki-bakeoff-code-2026-09/test_runtime_boundary.py --baseline .gobby/test-types-baseline.json --fail-on-new
uv run gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json
```

Suppression audit: 218 baseline, zero new/stale. Tests cover corpus and artifact containment,
same-size tampering, invalid installation logs, foreign/stale/shared-store grants, exact concurrent
trust attribution, unexpected global/config changes, service boundaries, and revision-preserving
preparation. `receipts/image-probe-regression-12124.json` records the isolated storage regression.
`logs/install/daemon-preparation-12124-resume{1,2}.log` record successful private preparation reruns.
Understand's native parser smoke passed 116 tests (`receipts/understand-anything-parser-smoke.json`),
including expected negative YAML/JSON parser cases. Native third-party compiler warnings remain
visible in their build logs; frozen sources were not patched to hide them.

Teardown remains deliberately pending for subsequent bakeoff tasks. Stop only the recorded owned
identities after verifying current PID, cwd, executable/argv, and listener ownership. Next.js changes
its process title to `next-server (v16.3.2)`; the validator checks its Node executable, exact private
cwd, and sole loopback listener. Remove Docker containers by recorded IDs after checking labels and
mounts; include the retained, exited `<project>-21942-opendeepwiki-build` container if cleaning build
artifacts. Remove only the three recorded owned volumes and network after dependent containers stop.
Retain the runtime's receipts, inputs, logs, and state until evidence delivery. Never remove shared
Gobby/Game Goblins containers, volumes, global files, or trust entries. Launchers require a fresh
attempt name, check free ports and runtime ownership, and retain immutable launch records.

## Gobby findings resolved during provisioning

Linked commit **`9627e78467`** fixes leading-global-flag gcode navigation classification,
`rg --files` directory normalization, and live transcript processor registration when SessionStart
was missed. The installed navigation rule was already enabled and correct; no bundled rule changed.
After coordinated restart, main daemon PID 65244 passed subsystem health checks. The external archive
search succeeded through the fixed hook. Focused navigation/rule tests passed (35), transcript/hook
checks passed (70), and the broader three-file hooks selection passed (377), with scoped mypy/Ruff
and no new test-type or quality findings. The existing 141 test-type baseline errors in the rule-test
file were unchanged.

Managed read-only Sol/xhigh worker `a84ed6cd-c5d3-4bb5-86d6-005c67a2d81c` completed successfully,
16 tool calls / 5 turns, no edits. While active, it already showed 23 messages / 7 tool calls, proving
live accounting. Child #12128's agent-end handoff was consumed. Earlier failed bootstrap/accounting
attempts remain diagnostic history; no failed run is counted as generation evidence.

All #21942 implementation substeps are complete. The bounded task close review remains the final
lifecycle step. Subsequent compatibility and baseline tasks must establish actual provider behavior,
embedding readiness, and native outputs; this environment acceptance makes no generation claim.
