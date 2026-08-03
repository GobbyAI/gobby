# Gobby gwiki Research Boundary

Research is agent work. The `gwiki research` verb is retired and must not be
reintroduced; `gwiki` owns the deterministic mechanics that research passes
consume — guarded URL ingest, SourceManifest content-hash dedup, accepted-note
collection, grounded `compile`, and `audit`.

The daemon exposes the deterministic wiki tools; it does not bundle a
`wiki-research` pipeline, launcher skill, or `wiki-researcher` agent. There is
no reserved research dispatcher or cron job. A caller that needs a research
pass owns its orchestration: it may create an ordinary task, discover sources
with its available web tools, then hand URLs and notes to `gwiki` through the
daemon's wiki tools (`wiki_ingest`, `wiki_compile`, and related operations).
Scheduled research likewise requires an explicitly installed caller-owned
workflow and cron action.

gwiki must not call back into Gobby as an agent-spawn dependency. The old
shape where `gwiki research` posted to `/api/agents/spawn` with `--task-id`,
`--agent-count`, or `--resume` is retired, along with the verb itself and the
legacy `wiki:research:<scope>` cron sweep.
