# Salvage Plan: 0.3.3-fail + 0.4.0-fail → Clean 0.3.3

## Context
After v0.3.2 release, two parallel branches (0.3.3, 0.4.0) accumulated ~37 and ~21 commits respectively. The subagent hooks work (#11218) triggered a cascade of SDK/session_id changes that broke the task validator. We need to get back to v0.3.2 baseline and cherry-pick only the good work.

## Step 1: Archive failed branches

```bash
git checkout 0.3.3
git branch -m 0.3.3 0.3.3-fail
git push origin 0.3.3-fail

git checkout 0.4.0
git branch -m 0.4.0 0.4.0-fail
git push origin 0.4.0-fail

# Create clean 0.3.3 from v0.3.2 tag
git checkout -b 0.3.3 v0.3.2
```

## Step 2: Commit classification

### TOXIC CHAIN — DO NOT CHERRY-PICK
All related to subagent hooks, session_id removal, and SDK fix cascade:

| Commit (0.3.3) | Task | Why toxic |
|---|---|---|
| `99377414c` | #11218 | is_subagent variable — started the chain |
| `d24d0ef5f` | #chore | subagent hooks + litellm upgrade (litellm upgrade needed separately) |
| `c559705a0` | — | reset is_subagent on user turn (depends on subagent feature) |
| `b72fa4f1b` | #11214 | remove session_id from stdio proxy — broke set/get_variable |
| `4451d7a97` | #11215 | remove stale session_id references |
| `000334f1a` | #11235 | remove redundant session_id from tool calls |
| `b55a28e3a` | #11235 | expand session_id cleanup to all internal tools |
| `a7a0e70f0` | #11242 | remove stale session_id kwargs |
| `e0a976859` | #11232 | consolidate SDK calls with GOBBY_INTERNAL |
| `6467de53b` | #11232 | restore CLAUDECODE=1 in SDK subprocess |
| `7829ab519` | #11231 | pin claude-agent-sdk<=0.1.45 |
| `9bdb7e188` | #11243 | stop re-injecting CLAUDECODE env var |
| `184a1e964` | #11243 | add --verbose to diagnostic CLI probe |
| `5a20e9e5c` | #11243 | pass --bare to SDK CLI subprocess |
| `4c3a205ea` | #11246 | add session_id param back to stdio proxy |
| `64ec3a595` | #11247 | remove --bare flag, include .sql in wheel |
| `cc656088a` | #11247 | bump claude-agent-sdk to >=0.1.56 |
| `121640b3e` | #11238 | remove source gate on handoff parent |

0.4.0-only toxic:
| `ff732551f` | — | suppress ghost claude_sdk sessions (caused by subagent changes) + neo4j auth (cherry-pick neo4j part separately) |
| `8ddf86c81` | — | v186 migration for agent mode renames (depends on SpawnMode rename) |

### SAFE TO CHERRY-PICK

| Commit (0.3.3) | 0.4.0 equiv | Description |
|---|---|---|
| `135457150` | `2fe1e8c83` | fix: codex-rescue agent template mode self → interactive |
| `35052d533` | `df50a9f39` | fix: suspend AudioContext on unmount instead of closing |
| `8706785e8` | `69c8e41bb` | refactor: eliminate skill template rows from DB |
| `8591e37ca` | `1ddcd134b` | feat: doom loop detection + shadow git checkpoints |
| `44c5a87e2` | `1f6ceadb5` | test: dispatch_failure_count CRUD, reopen reset, loop escalation |
| `6017a5581` | `1549e0846` | fix: use escalated not blocked for dispatch failure recovery |
| `e40fc0571` | `f38423aae` | feat: add phase subepic support to task expansion |
| `c8d4865cd` | — | fix: resolve neo4j auth secret for docker compose |
| `f9f319717` | — | fix: move neo4j password to bootstrap.yaml |
| `6df82c527` | `ab6ca6fc6` | chore: reorganize plans, rename license, clean up docs |
| `f1cb09683` | — | fix: remove dead templates filter branch in SkillsPage |
| `d8470e619` | — | fix: suppress llama.cpp stderr spam |
| `46d74ea26` | — | fix: suppress GGML log spam during embedding init |
| `43cdb4a1b` | — | fix: use LocalProjectManager for session project path |
| `5ba6497e8` | — | fix: use self._db for project path in lifecycle_monitor |
| `e4b04524b` | `cc4713e2f` | fix: validate project_path before git subprocess |
| `6c1be5ef4` | `f6d3e84b4` | refactor: remove dead executor layer, streaming, in_process |
| `92a0321ff` | — | chore: update mode='terminal' refs → interactive/autonomous (CRITICAL) |
| `2f6de0f07` | — | fix: align build_cli_command mode checks with SpawnMode rename (CRITICAL) |

### NEEDS SEPARATE COMMITS (not cherry-pick, extract from mixed commits)

1. **litellm upgrade** — bundled in `d24d0ef5f` (subagent hooks commit). Extract the litellm version bump only. Fixes CVE.
2. **storage/*.sql in pyproject.toml** — from today's `64ec3a595`. Need standalone commit.
3. **SDK version bump** — from today's `cc656088a`. Needs to be >=0.1.56 to work with CLI 2.1.92.
4. **Codex review gate deprecation** — from 0.4.0 `24c4d5367`. Blocker if not removed.
5. **Neo4j auth from ghost sessions commit** — `ff732551f` mixes ghost session suppression (toxic) with neo4j auth fix (safe). May need to extract just the neo4j part if `c8d4865cd` + `f9f319717` don't fully cover it.

### SKIP (no code value)

| Commit | Description |
|---|---|
| `aede1fcec`, `e459b6a72` | gobby: sync tasks/memories |
| `587725916` | bump version to 0.4.0 |

## Step 3: Cherry-pick order

Start from v0.3.2 baseline. Order by dependency — foundational fixes first, features second:

```bash
# Infrastructure / mode rename (CRITICAL — many things depend on these)
git cherry-pick 92a0321ff  # update mode='terminal' refs → interactive/autonomous
git cherry-pick 2f6de0f07  # align build_cli_command mode checks

# Bug fixes
git cherry-pick 135457150  # codex-rescue agent mode fix
git cherry-pick 35052d533  # AudioContext suspend fix
git cherry-pick 6017a5581  # escalated not blocked for dispatch failure
git cherry-pick c8d4865cd  # neo4j auth secret for docker compose
git cherry-pick f9f319717  # neo4j password to bootstrap.yaml
git cherry-pick f1cb09683  # dead templates filter SkillsPage
git cherry-pick d8470e619  # suppress llama.cpp stderr
git cherry-pick 46d74ea26  # suppress GGML log spam
git cherry-pick 43cdb4a1b  # LocalProjectManager for session project path
git cherry-pick 5ba6497e8  # self._db for project path in lifecycle_monitor
git cherry-pick e4b04524b  # validate project_path before git subprocess

# Refactors
git cherry-pick 8706785e8  # eliminate skill template rows from DB
git cherry-pick 6c1be5ef4  # remove dead executor layer

# Features
git cherry-pick 8591e37ca  # doom loop detection + shadow git checkpoints
git cherry-pick 44c5a87e2  # dispatch_failure_count tests
git cherry-pick e40fc0571  # phase subepic support

# Docs/cleanup
git cherry-pick 6df82c527  # reorganize plans, rename license, clean up docs
```

Then standalone commits for:
- litellm version bump (extract from d24d0ef5f)
- storage/*.sql in pyproject.toml package-data
- SDK version bump >=0.1.56
- Codex review gate deprecation (extract from 24c4d5367 or write fresh)

## Verification

1. `uv sync`
2. `uv run ruff check src/` — lint clean
3. `uv run gobby start --verbose` — daemon starts
4. `uv run pytest tests/ -x --timeout=60 -q` — spot-check
5. Test validator: create task, make change, close_task with commit_sha — MUST work
