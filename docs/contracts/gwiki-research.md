# Gobby gwiki Research Boundary

Research is agent work. The `gwiki research` verb is retired and must not be
reintroduced; `gwiki` owns the deterministic mechanics that research passes
consume — guarded URL ingest, SourceManifest content-hash dedup, accepted-note
collection, grounded `compile`, and `audit`.

The research loop lives daemon-side:

- One research pass = one execution of the bundled `wiki-research` pipeline
  (`src/gobby/install/shared/workflows/pipelines/wiki-research.yaml`): create
  a research task, spawn the `wiki-researcher` agent, wait for a cited topic
  page. See `docs/guides/wiki-research.md` for submit/list/pause/edit flows.
- Standing queries are ordinary cron jobs with `action_type: pipeline`
  pointing at `wiki-research`. Cron owns the name, schedule, enablement, and
  history; there is no separate research registry.
- The researcher agent discovers sources with its native web search and hands
  URLs/notes to `gwiki` through the daemon's wiki tools (`wiki_ingest`,
  `wiki_compile`); it never shells into wiki mutation paths of its own.

gwiki must not call back into Gobby as an agent-spawn dependency. The old
shape where `gwiki research` posted to `/api/agents/spawn` with `--task-id`,
`--agent-count`, or `--resume` is retired, along with the verb itself and the
legacy `wiki:research:<scope>` cron sweep.
