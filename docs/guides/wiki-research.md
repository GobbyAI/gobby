# Wiki Research Pipeline

One research pass = one execution of the bundled `wiki-research` pipeline: it
creates a research task from a natural-language question, spawns the
`wiki-researcher` agent against it, and waits for the agent to compile a cited
topic page into the wiki vault. Research is agent work; `gwiki` supplies the
mechanics (guarded URL fetch, content-hash dedup, grounded compile, audit).
There is no separate research registry or `gwiki research` verb — ad-hoc runs
are pipeline executions, and standing queries are ordinary cron jobs pointing
at the pipeline.

## What a run produces

- A research task (category `research`, label `wiki-research`) tracking the
  pass.
- Ingested sources in the vault (`knowledge/sources/…`) with SourceManifest
  entries; re-fetching an unchanged URL dedups against the manifest instead of
  creating new entries.
- A compiled, cited topic page at `knowledge/topics/<slug>.md` (explicit
  topic + full source list).
- A run report page and a `log.md` append.
- Optional follow-up investigation tasks (label `wiki-research`,
  `allow_automation` untouched) when `create_tasks` is `"true"`.

Only one pass runs at a time per project: a second concurrent execution fails
fast with `Skipped: another wiki-research execution is already running`
(concurrent passes would race on the research checkpoint and the shared
source manifest).

## Submit an ad-hoc run

CLI:

```bash
gobby pipelines run wiki-research \
  --input question="Pull every new paper from arxiv published in the last 24 hours ..." \
  --input topic_slug="" \
  --input max_sources=12 \
  --input max_items=8 \
  --input create_tasks=true \
  --input provider=claude \
  --input model=""
```

MCP: the pipeline is exposed as the dynamic tool `pipeline:wiki-research` on
`gobby-workflows` (progressive discovery: `list_tools("gobby-workflows")` →
`get_tool_schema` → `call_tool`). `run_pipeline` with
`name="wiki-research"` is equivalent.

Inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `question` | (required) | Free-form research question. May embed a custom output contract, which overrides the wiki-research skill's default note/report format. |
| `topic_slug` | `""` | Explicit topic page slug (`knowledge/topics/<slug>.md`); empty derives one from the question. |
| `max_sources` | `12` | Hard discovery cap. |
| `max_items` | `8` | Hard curation/ingest cap. |
| `create_tasks` | `"true"` | File follow-up investigation tasks for open leads. |
| `provider` | `claude` | Researcher agent provider passthrough. |
| `model` | `""` | Model override; empty uses the wiki-researcher agent default. |

The question is source-agnostic: arxiv, Hacker News, law reviews, RSS-style
feeds — discovery happens through the researcher agent's native web search,
so no code changes are needed for new source families.

## List runs and inspect status

```bash
gobby pipelines runs list            # recent executions
gobby pipelines runs show <id>       # step-by-step status for one execution
```

MCP equivalents on `gobby-workflows`: `list_pipeline_executions`,
`search_pipeline_executions`, `get_pipeline_status`. A running execution can
be stopped with `gobby pipelines runs cancel <id>` / `cancel_pipeline` (this
also kills the spawned researcher agent).

## Standing queries (cron)

A standing query is a cron job with `action_type: pipeline` — cron owns the
name, schedule, enablement, and history:

```bash
# Submit
gobby cron add \
  --name wiki-research-nightly-arxiv \
  --schedule "0 6 * * *" \
  --action-type pipeline \
  --action-config '{"pipeline_name": "wiki-research", "inputs": {"question": "Pull every new paper from arxiv published in the last 24 hours ...", "max_sources": 12, "max_items": 8}}' \
  --description "Nightly arxiv sweep for gobby-relevant papers"

# List / inspect
gobby cron list
gobby cron runs wiki-research-nightly-arxiv

# Trigger immediately (does not change the schedule)
gobby cron run wiki-research-nightly-arxiv

# Pause / resume
gobby cron toggle wiki-research-nightly-arxiv

# Edit (schedule, question, budgets, …)
gobby cron edit wiki-research-nightly-arxiv

# Retire
gobby cron remove wiki-research-nightly-arxiv
```

Cron-triggered executions run under a dedicated `cron:<job-name>` session
parented to the system session, so spawned researcher agents always have a
valid parent. The re-entrancy guard applies to cron runs too: if a manual run
is still in flight when the schedule fires, the scheduled pass skips instead
of racing it.

See `docs/guides/cron-scheduler.md` for general cron behavior and
`docs/guides/pipelines.md` for pipeline execution semantics.

## Verify a pass

- `gwiki sources` lists the fetched URLs and note sources.
- `knowledge/topics/<slug>.md` exists and carries `[source: …]` citations.
- `gwiki audit` (or `wiki_audit` over MCP) must not flag the topic page.
- Re-running the same question re-uses unchanged sources (no new manifest
  entries) and writes a fresh run report.
