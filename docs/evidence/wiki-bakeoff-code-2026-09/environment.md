# Code-wiki bakeoff environment

Status: **host provisioning in progress** on 2026-09-07. Task #21942 remains open. The user
clarified that the coordinator should provision host resources before using sandboxed workers.
No daemon-level sandbox permission changes are needed for that host-owned setup phase.

## Coordinator setup checkpoint

- Host `docker version --format '{{json .}}'` succeeded against Docker Desktop. Host GitHub
  access succeeded; Graphify HEAD resolved to the frozen `c9f99018774e2e0380e9f65b3959944559a0d5f6`.
- Created `/Users/josh/Projects/wiki-bakeoff-code-2026-09` with mode `0700`, owner task `#21942`,
  and owner session `#12034`. No pre-existing runtime was overwritten.
- Archived both Game Goblins commits and pinned Gobby source; created all 69 independent corpus
  copies, exclusion inventories, hashes, private service credentials, and before-state evidence.
  `validate_manifests` passed for every copy, including secret-pattern and input-exclusion checks.
- The three owned service containers are running and healthy. Rendered configuration and live
  image, label, network, named-volume, and loopback binding checks pass. Their actual IDs are in
  runtime `receipts/ownership-live.json`; service identities are in `receipts/services.json`.
- Pinned gcode and gdaemon release builds passed. Graphify and CodeWiki installed in separate
  virtual environments and report the approved versions. Archify dependencies and Understand
  Anything's locked workspace dependencies/core build passed. Project-local skill activation and
  complete installation receipts remain pending. Grok Wiki's DMG hash and copied app signature
  passed; it is installed only inside the runtime, not `/Applications`.
- The private daemon bootstrap and schema/config seed passed. No isolated daemon has started;
  the live signed-grant path is still unverified. No model generation has run.
- Fixed the unexecuted provisioning scaffold's stale owner ID, private-root permissions, mutable
  image acceptance, mismatched Compose volume names, and implicit build step. Seven offline
  regression cases pass; Ruff, test-quality, test-types, and suppression checks passed.
- Direct Git cloning was blocked by the repository's managed-clone rule. The supported
  `gobby-clones.create_clone` then rejected this runtime location with `clone_path_outside_root`.
  No unmanaged clone was created. Official commit source archives were acquired and checked with
  `extract_sources.py` instead; no clone/sandbox settings changed.

## Host installation and service evidence

Official archives were downloaded from `https://codeload.github.com/<owner>/<repo>/tar.gz/<pin>`.
`extract_sources.py` rejects unexpected prefixes, traversal, symlinks, special files, oversized
archives, and existing destinations; it records per-file source manifests and archive hashes.
All five archives extracted successfully:

| Source | Files | Archive SHA-256 |
| --- | ---: | --- |
| Graphify | 869 | `fda859ebf8e3cfc08cf5d51de70d21cf9bce09bf5517a3f287935dd3ff870de4` |
| Understand Anything | 514 | `cd479aeb19f4661e4999bf977f1cc757bed02ead743db5ed09cc9152cc3a71b9` |
| Archify | 473 | `0993116cee2add78ff1cc00e50dd60715e5886fbf76a678a6ce4f99f9f034be8` |
| CodeWiki | 163 | `9eee49cc531a563b32a2fd2894c9313b6f8d4449e0c27485520dada6e9a10565` |
| OpenDeepWiki | 912 | `5c46ae750e884fbafe4eaabaa5e785d5b0de2428a5f5a04082e121065d29944c` |

Source receipts/manifests are `receipts/<comparator>-source.json` and
`manifests/<comparator>-source.json`. No claim is made that the Graphify PyPI sdist is byte-identical
to its commit archive: this installation uses the official pinned source with its frozen `uv.lock`.

Executed host operations (all paths below are within the approved runtime):

- Built `sources/gobby/Cargo.toml` with `cargo build --release --locked -p gobby-code
  -p gobby-daemon -j 6` and `CARGO_TARGET_DIR=<runtime>/build/gobby-target`; exit 0, 9m14s.
  Installed copies into `tools/gcode/bin/gcode` and `gobby-home/bin/{gcode,gdaemon}`.
  gcode SHA-256: `1f64d7400001a890ab6630ea7a823d8dbaf7e6b18513d3bb3bed6a07075f7652`;
  gdaemon SHA-256: `d34e0bf08e70e2d4f38076f03950348e711a2567e6e56ff52437551d8b40e0b4`.
- Graphify: `uv sync --frozen --no-dev --extra mcp --extra svg`, Python 3.13.15,
  `UV_PROJECT_ENVIRONMENT=<runtime>/tools/graphify/venv`; `graphify --version` → `0.9.55`.
- CodeWiki: resolved `pyproject.toml` with `uv pip compile` into
  `build/codewiki-requirements.txt`, synchronized a private Python 3.12.14 environment, then
  installed the pinned source with `uv pip install --no-deps --editable`; CLI reports `1.0.1`.
  The dependency lock records the resolved Git-based coding-agent-wrapper dependency.
- Archify: `npm ci --ignore-scripts --no-audit --no-fund` in `sources/archify/archify`; exit 0.
- Understand Anything: private `pnpm@10.6.2`, frozen-lockfile install with scripts initially
  disabled, then `pnpm --filter @understand-anything/core build`; exit 0. Upstream warns that
  the nested `pnpm.onlyBuiltDependencies` field has no effect; root workspace policy remains
  authoritative. Native grammar rebuild/skill activation still require verification.
- Grok Wiki: downloaded release tag `0.0.38` (not `v0.0.38`), verified the approved DMG hash,
  mounted read-only, copied the app using `ditto`, passed `codesign --verify --deep --strict`,
  then detached the owned mount. No app launch or global installation.
- OpenDeepWiki: source acquired, but not built or started. .NET SDK image acquired as
  `mcr.microsoft.com/dotnet/sdk@sha256:4beef5b8919dcaa2dc924233bd069257e883cc7a061e09088a97d152d6a48510`.
  The upstream Compose file is not safe to launch unchanged and has not been launched.

`launch_services.py start --owner-session '#12034'` started only the three owned services after
checking the rendered configuration, immutable local image references, collisions, and free ports.
Docker startup succeeded; the initial observer incorrectly rejected Docker's named-volume entries
in `HostConfig.Binds`. After correcting that representation check,
`launch_services.py observe --owner-session '#12034'` passed. The original start log is retained
at `logs/install/services-start.json`.

Pre-launch render tests also caught and fixed PostgreSQL's comma-split preload argument and the
folded Qdrant HTTP healthcheck. The generated Compose file now checks exact commands, environments,
healthchecks, networks, volumes, ports, and repository digests. Rendered configuration contains
credentials and is kept private at `receipts/compose.rendered.json`, mode `0600`; do not publish it.

`prepare_daemon.py --owner-session '#12034'`, run through the pinned Gobby environment, initialized
the database and configuration using normal Gobby schema/config mutation interfaces. The reused
image creates `public._pgaudit_probe`, so the normal schema authority correctly rejected `public`
as nonempty unknown lineage. The probe was preserved; Gobby now uses dedicated schema
`bakeoff_21942` through the DSN search path. The initial bootstrap was preserved as
`config/bootstrap.initial-public.yaml`; the active private bootstrap is `gobby-home/bootstrap.yaml`.
`receipts/daemon-preparation.json` records success. Extensions observed in the owned database:
`pg_search 0.23.4`, `pgaudit 18.0`.

The child setup environment clears inherited managed identity, datastore overrides, provider keys,
and telemetry. It explicitly sets `GOBBY_HOME` and **`GOBBY_NATIVE_BIN_DIR`**; the latter is required
because native binary resolution otherwise defaults to the user's real managed bin directory.
It does not reassign `HOME` or `CODEX_HOME`. Runtime configuration uses canonical
`ai.embeddings.*` keys, not removed `embeddings.*` storage keys.

## Remaining owned checks

The read-only Sol/xhigh audit identified additional work still owned by #21942:

- Complete installation receipts and validate actual executable/source/lock/log bytes for every
  comparator, rejecting escaped/symlink paths. Remove the obsolete hard-coded Graphify
  `commit_byte_identity == UNVERIFIED` acceptance condition.
- Start the isolated daemon; verify process identity, bootstrap endpoints, current signed grant,
  expiry/binding, and each backend through the actual gcode path. Self-authored receipt booleans
  and a nonempty `project_id` are not sufficient proof.
- Finish before/after isolation checks against captured existing containers, volumes, listeners,
  and global files, accounting explicitly for concurrent unrelated activity.
- Finish hardening all resumable command paths and ownership checks. New helpers reject changed
  evidence overwrites; `refresh-manifest` now writes observations separately and preserves frozen
  manifests. Corpus scanning now rejects symlinks and scans all sizes in bounded chunks.
- Resolve the search-hook scoping/fail-open finding: broad searches naming unregistered external
  archives are redirected to gcode even after it returns `checkout_required`. The diagnostic was
  sent to active project sessions; no owner has accepted it yet. Keep this finding owned here.

No provider-native tracker is exposed in this session; this ledger and the task handoff retain the
implementation substeps. The audit helper made no edits, ended through its supported blocker
handoff, and the coordinator reclaimed #21942. It must not be treated as environment completion.

Executed initialization:

```bash
GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1 uv run python \
  docs/evidence/wiki-bakeoff-code-2026-09/provision_environment.py init --owner-session '#12034'
```

The original sandboxed-worker preflight below is historical evidence, not the current host state.

## Frozen inputs verified

- Gobby HEAD at task start: `26862c129335ddebdab413ba262a848d319bbbb8` (the closed
  matrix commit).
- Pinned Gobby/gcode object:
  `7394b97c1d88c82f685e788e798de2cfd728ad15`; the existing shared
  `~/.gobby/bin/gcode` reported `gcode 1.7.0` and SHA-256
  `2b04a055ba33d328ab4565db3b56ec7a164ac3c25fe4d496e33438effea7cae7`.
  This observation is not installation evidence and the shared binary was not modified.
- Game Goblins baseline and change objects resolved exactly to
  `0216f1e33f05962d49467d95fe84609041c6dba8` and
  `8b24ac26699aac8b24254a647aa70b208287b492`.
- The live Game Goblins checkout remained at
  `df9a90bf246f5ed294cefae36132b203c24daa79`. Its pre-existing untracked ZIPs,
  plan, and `var/` data were observed and left untouched.
- The frozen matrix validator passed before provisioning:

  ```text
  DATABASE_URL=postgresql://gobby_bakeoff_21942:REDACTED@127.0.0.1:61234/gobby_bakeoff_21942 \
    GOBBY_TEST_PROTECT=1 uv run python \
    docs/evidence/wiki-bakeoff-code-2026-09/validate_matrix.py \
    --source-repo /Users/josh/Projects/game-goblins
  matrix validation passed
  exit 0
  ```

The three tracked `.env.example` files were read directly from the baseline Git object. Their
credential fields are empty and their database URLs contain only loopback example identities; no
secret value was found. The planned corpus exclusions are `.gobby/project.json` and
`.gobby/mcp/servers/lightspeed.yaml`, plus all untracked/runtime/generated material.

## Isolation preflight

The required runtime root `/Users/josh/Projects/wiki-bakeoff-code-2026-09` did not exist. Shared
ports `60891`, `60892`, `6333`, `6334`, `16379`, and `13000` were occupied. Proposed owned ports
`59480`, `59481`, and `61234` through `61239` were free. The harness reserves distinct container,
volume, network, database, credential, state, and daemon names; it never changes `HOME`,
`CODEX_HOME`, shared binaries, or global configuration.

The managed SRT policy prevented the first runtime write:

```text
Path.mkdir('/Users/josh/Projects/wiki-bakeoff-code-2026-09')
PermissionError: [Errno 1] Operation not permitted
```

Docker inspection was also denied before any container mutation:

```text
docker version --format '{{json .}}'
docker ps --format '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}'
docker volume ls --format '{{.Name}}'
permission denied while trying to connect to the docker API at
unix:///Users/josh/.docker/run/docker.sock
```

The effective policy hash was
`f111cae850802dab2a7d86daa38d4cdb62c8992bfc863f6e3fc0375b2b63e943` with
`allow_git_network=false`, `allow_package_registries=true`, no Unix-socket grants, and write paths
limited to the checkout plus run-local temporary/cache paths. The daemon records this run under
`~/.gobby/runtime/managed-executions/<agent-run-id>/`, with policy at `assets/settings.json` and
violations at `logs/violations.jsonl`.

## Comparator installation dispositions

No comparator was installed because the required runtime root was unwritable.

| Comparator | Required pin | Preflight disposition |
| --- | --- | --- |
| Graphify | `c9f99018774e2e0380e9f65b3959944559a0d5f6`, package `0.9.55` | PyPI metadata was reachable. The known sdist SHA-256 remains `8135a5a22b6b78745aa3ab040cb3f5cecd7126eef5b4e89764404e6e75b58568`; commit byte identity remains **UNVERIFIED**. GitHub returned 403. |
| Understand Anything | `07edf82a04371b6f69779b067bdc8a1a8753a9db` | GitHub `git ls-remote` blocked: `CONNECT tunnel failed, response 403`. |
| Archify | `c6519401f7b91b9d43011657880893b0a8955548` | GitHub `git ls-remote` blocked: `CONNECT tunnel failed, response 403`. |
| CodeWiki | `2584854d7538dc3e3e8e6839cf8590b0cd12a431` | GitHub `git ls-remote` blocked: `CONNECT tunnel failed, response 403`. |
| OpenDeepWiki | `75840e5e86213ca40ace9d5036b1f52603f8d038` | GitHub `git ls-remote` blocked: `CONNECT tunnel failed, response 403`; `dotnet` was unavailable. |
| Grok Wiki | release `0.0.38`, DMG SHA-256 `bbcccef258102a1f32e36947b1a6da059a828bf1cea552dfdb58ca35ab562e09` | GitHub API blocked by allowlist with HTTP 403; no global install attempted. |
| gcode | Gobby `7394b97c1d88c82f685e788e798de2cfd728ad15`, `1.7.0` | Local Git object verified; build could not start because its owned target path was unwritable. |

The five Git checks used their exact approved HTTPS origins and all failed once with the same 403.
No retry, mirror, package substitution, version update, or sandbox bypass followed.

## Remaining host setup

Do not rerun `init` or `launch_services.py start`: the owned runtime and services now exist. Finish
the remaining checks above, pinned dependency activation, isolated daemon launch, and live
signed-grant evidence. The coordinator performs host operations;
sandboxed workers do not receive Docker or broad network permissions. Assess their remaining
workspace access only after setup. Until installation and live isolation checks pass, acceptance
items 1.2.1 through 1.2.3 remain incomplete and this task must not close.
