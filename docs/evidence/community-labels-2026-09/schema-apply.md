# Migration 443 Apply Evidence

Recorded at `2026-09-20T01:25:00Z` (`2026-09-19T20:25:00-0500 CDT`).
The community schema landed as migration **443**.

## Committed inputs

- Schema implementation: `d1271c80f0043214fc808aac12bd396d2a9cab52`
- `cutover --path` candidate-pin fix: `bce7d1a`
- Cutover restart-pin propagation fix: `00d7b9b`
- Baseline version: `420`
- Baseline SHA-256: `f8e4cea2f63769a2fd2b32a93a56574c4fda3d335a745aa0970cfea6a2596b55`
- Baseline Git blob: `5e3abb1b6471be34ea35f10cae43ec265b260747`
- Migration checksum: `ff143a0c2d040ebd44b5138e717608705e81d42176dece35561802fcd1e06229`
- Assets root hash: `922c48b11dd187b59167a0d3c19a43beb1380ca5fd428c8d4833312a64cef7aa`

## Pre-apply plan

The freshly built lane release binary reported:

```text
schema public plan: database v442, code v443, baseline_pending=false, pending_migrations=1 [443]
```

## Coordinated cutover

After a global announcement and a zero-active-agent/validator check, cutover ran
from `/Users/josh/Projects/gobby`:

```text
uv run gobby cutover --path /Users/josh/.gobby/worktrees/gobby/lane-22581-gcode-import-communities
```

The first preflight safely refused before promotion because `--path` compared the
candidate with the invoking checkout's v442 pin. The focused fix made candidate
preflight use the selected workspace pin. A second attempt promoted the coherent
v443 set, then the nested restart safely refused because its schema-apply check
still used the invoking checkout pin. The focused restart-propagation fix carried
the validated lane identity through that check without changing ordinary restart
behavior. Both defects have regression coverage.

The final cutover stopped PID `88619`, started the service-managed daemon as PID
`71740`, initialized every reported subsystem, and ended with:

```text
+ Health check passed (12.8s)
Cutover complete: gcode, gdaemon, ghook, schema pin, and daemon agree.
```

## Post-apply proof

Installed schema plan:

```text
schema public plan: database v443, code v443, baseline_pending=false, pending_migrations=0 []
```

Installed `gdaemon schema version --json`:

```json
{"runner_protocol":1,"baseline_version":420,"baseline_checksum":"f8e4cea2f63769a2fd2b32a93a56574c4fda3d335a745aa0970cfea6a2596b55","latest_version":443,"latest_checksum":"ff143a0c2d040ebd44b5138e717608705e81d42176dece35561802fcd1e06229","assets_root_hash":"922c48b11dd187b59167a0d3c19a43beb1380ca5fd428c8d4833312a64cef7aa"}
```

Hashes read from the promoted, ad-hoc-signed files in `~/.gobby/bin/`:

```text
9aefcbc6c7e2f03dc6211ec526192fc839b0a7e790d942db55e9060070a85562  /Users/josh/.gobby/bin/gcode
8dba3a24f86569f7bbe96be33dea01928f5050754937518b15b79e664656f81f  /Users/josh/.gobby/bin/gdaemon
6f1a70b8ca7587b682b6ef2ce2266bd6153c6a43cea35d4d98fa16ca40b6112c  /Users/josh/.gobby/bin/ghook
```
