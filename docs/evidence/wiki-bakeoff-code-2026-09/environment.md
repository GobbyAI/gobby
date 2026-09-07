# Code-wiki bakeoff environment

Status: **blocked before provisioning** on 2026-09-07. Task #21942 remains open. No substitute
runtime, comparator version, service, or global installation was used.

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

## Resume requirements

A resumed worker needs explicit supported grants for exactly the runtime root, Docker's Unix
socket, and the approved GitHub origins. It must run `provision_environment.py init`, resolve and
pin image digests, render and reject unsafe Compose configuration before launch, build the pinned
gcode into the runtime, start an isolated Gobby daemon, and produce passing live signed-grant
evidence with `validate_environment.py`. Until those checks pass, acceptance items 1.2.1 through
1.2.3 remain incomplete and this task must not close.
