# gwiki CodeWiki Guide

`gwiki code` owns CodeWiki generation. The direct CLI is available for
isolated/manual use, including parity checks and work in disposable vaults.
Production-vault execution is operationally paused pending the wiki redesign.
Daemon scheduling, post-commit refreshes, and UI refresh controls remain disabled.

The retired invocation and the legacy `/api/code-index/codewiki/*` routes have
been removed. Use the `gwiki` command and the `/api/wiki/code/*` status contract
described below.

## Operational State

Production automation must treat CodeWiki as disabled:

- The daemon does not schedule nightly or post-commit generation.
- Startup reconciliation disables persisted CodeWiki cron jobs.
- The web UI fetches status once, shows a **Paused** badge and reason, and offers
  no refresh control.
- Direct CLI execution does not re-enable daemon orchestration.

Six `WikiConfig` keys remain stored so configuration can survive the redesign.
Every key currently has no effect:

- `wiki.codewiki_on_commit`
- `wiki.codewiki_nightly_enabled`
- `wiki.codewiki_nightly_schedule_cron`
- `wiki.codewiki_nightly_timezone`
- `wiki.codewiki_scopes`
- `wiki.codewiki_project_scopes_by_name`

## Direct Manual CLI

Use an explicit project and output path for isolated/manual use:

```bash
gwiki --project /path/to/project code --out /path/to/project/wiki
```

The canonical production vault remains `<project-root>/wiki`. Avoid writing to
that vault while production execution is paused; use a temporary output path for
validation and parity work.

Common manual modes:

```bash
# Limit generation to indexed files below selected paths.
gwiki --project /path/to/project code \
  --scope crates/gwiki src \
  --out /tmp/codewiki-check

# Regenerate pages affected by changes since a Git ref.
gwiki --project /path/to/project code \
  --since HEAD~1 \
  --out /tmp/codewiki-check

# Re-anchor citations without generating prose.
gwiki --project /path/to/project code \
  --repair-citations \
  --out /tmp/codewiki-check

# Compare current metadata with a published snapshot.
gwiki --project /path/to/project code \
  --compare-to 'HEAD:wiki/_meta/codewiki.json' \
  --format json
```

Generation accepts `--ai auto|daemon|direct|off`, `--ai-depth`,
`--ai-prose-depth`, `--ai-register`, and `--max-workers`. `--include-docs`
adds narrative Markdown and text inputs to the default code and structured
configuration set. `--no-freshness` bypasses the generation-path code-index
freshness check and should be reserved for controlled diagnostics.

## Dormant Daemon Routes

`GET /api/wiki/code/status` is the stable read contract. It returns HTTP 200:

```json
{
  "enabled": false,
  "state": "disabled",
  "reason": "pending_wiki_redesign"
}
```

`POST /api/wiki/code/refresh` is retained as an explicit disabled response. It
performs no work and returns HTTP 409:

```json
{
  "error": "codewiki_disabled_pending_redesign",
  "reason": "pending_wiki_redesign"
}
```

Clients should display the server-provided reason. Polling and retrying the
refresh route cannot change the state; `codewiki_disabled_pending_redesign` is
a terminal operational response until the redesign re-enables orchestration.

## Output Contract

A generation run writes vault-relative pages beneath `code/` and shared state
beneath `_meta/`:

```text
<out>/
├── code/
│   ├── INDEX.md
│   ├── _architecture.md
│   ├── _changes.md
│   ├── _hotspots.md
│   ├── _onboarding.md
│   ├── _ownership.md
│   └── ... per-module and per-file pages
└── _meta/
    └── codewiki.json
```

Generated pages use the shared `gobby_core::codewiki_contract` frontmatter and
ground source claims with `[file:line]` citations. Structural pages remain
available when optional graph, AI, or repository-ownership signals degrade;
frontmatter and command output report the affected sources.

Incremental generation uses source hashes, cross-file neighborhoods, generation
options, and the prior metadata snapshot. `--scope` selects inputs;
`--complete-scope` additionally treats those paths as the full publication
boundary and may remove generated pages outside it.

## Purge Safety

Purge removes generated CodeWiki Markdown and metadata beneath the selected
output directory. It does not clear PostgreSQL code facts, FalkorDB graph data,
or Qdrant vectors. The destructive action requires `--force`:

```bash
gwiki --project /path/to/project code \
  --purge \
  --out /tmp/codewiki-check \
  --force
```

Confirm the resolved project and output path before running it.

_Last verified: 2026-08-09_
