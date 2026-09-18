# Memory System Guide

Gobby's memory system stores durable project facts, user preferences, and
working conventions in the local hub database so future sessions can recall
them. It is separate from tasks, session transcripts, and native provider memory
files.

## Quick Start

```bash
# Store a user-authored memory. Without --project this uses the personal project.
gobby memory create "Use focused pytest files for task validation" --type preference \
  --rationale "The user wants future sessions to keep validation proportional to the change."

# Store a memory for a specific Gobby project.
gobby memory create "This project uses uv for Python commands" --type fact --project gobby \
  --rationale "An explicitly requested convention for future sessions in this project."

# Recall memories with semantic or FTS-backed search.
gobby memory recall "validation commands" --limit 5

# List and inspect memories.
gobby memory list --type preference
gobby memory show MEMORY_ID_OR_PREFIX
```

```python
# MCP tools are project-scoped by the current session context.
call_tool(server_name="gobby-memory", tool_name="create_memory", session_id="CURRENT_SESSION", arguments={
    "content": "User prefers task-linked commits.",
    "rationale": "The user explicitly requested this convention for future project work.",
    "memory_type": "preference",
    "tags": ["workflow", "commits"]
})

call_tool(server_name="gobby-memory", tool_name="search_memories", arguments={
    "query": "commit workflow",
    "limit": 5,
    "tags_any": ["workflow", "commits"]
})
```

For a known memory tool without a current-context lease, fetch its schema directly:

```python
get_tool_schema(server_name="gobby-memory", tool_name="search_memories")
call_tool(server_name="gobby-memory", tool_name="search_memories", arguments={...})
```

## What Belongs in Memory

Use memories for durable context that future agents need and cannot cheaply
derive from code or git history.

| Store as memory | Use another system |
| --- | --- |
| User preferences and workflow conventions | Bugs and failures: fix under the current task and repository found-work ladder |
| Design rationale that is not obvious in code | Current implementation state: read the code |
| External references that are hard to rediscover | Recent changes: use git log or linked commits |
| Stable cross-session facts about a project | One-turn instructions or temporary task notes |

Good memories are specific and time-resilient:

- "The project treats Markdown guide line count as documentation scope, not the source-file monolith rule."
- "Use `GOBBY_TEST_PROTECT=1` for pytest in this repo so tests cannot touch the production daemon state."
- "The memory backup file is a JSONL backup/export path, not the memory source of truth."

Avoid storing secrets, API keys, passwords, transient debug notes, duplicate
facts, or facts that are already obvious from source files.

## Concepts

### Memory Types

The public durable memory types are:

| Type | Use for |
| --- | --- |
| `fact` | Objective project or environment facts |
| `preference` | User preferences and durable workflow choices |
| `pattern` | Repeated conventions or design patterns |
| `context` | Broader project context that should be injected as prose |

MCP validates these values even where the exposed schema says string. The
special `implementation_note` create path skips ephemeral notes and normalizes
durable ones to context; it is not an additional durable type. Search results
carry the type so an agent can distinguish preferences from facts.

### Scope

Scope differs by surface:

| Surface | Scope behavior |
| --- | --- |
| MCP create/search/list | Use current project context, or the personal project if absent. |
| MCP maintenance | Scope is tool-specific; pass an explicit project where supported. Reindexing has no project parameter. |
| HTTP `/api/memories` routes | Accept explicit `project_id` query/body fields where supported. |
| CLI `gobby memory ...` | Use `--project` when you want project-scoped create, list, recall, show, delete, or stats behavior. |
| CLI without `--project` | Creates memories owned by the personal project; list/search can omit the project filter. |

Global visibility (`is_global`) is independent of project ownership (`project_id`).
Use `promote_memory_to_global`, `demote_memory_from_global`, or `move_memory`
for intentional changes. Seeing a foreign global memory does not grant mutation
rights; these MCP tools require current-project ownership. CLI and HTTP scope
are separate contracts, not agent ownership bypasses.

### Tags

Tags support boolean filters on both CLI and MCP surfaces:

| Filter | Meaning |
| --- | --- |
| `tags_all` / `--tags-all` | Memory must have every listed tag. |
| `tags_any` / `--tags-any` | Memory must have at least one listed tag. |
| `tags_none` / `--tags-none` | Memory must have none of the listed tags. |

Use tags for stable concepts such as `workflow`, `testing`, `security`,
`architecture`, `preference`, and `external-reference`.

## CLI Reference

### Create, Recall, List

```bash
gobby memory create "CONTENT" --rationale "DURABLE VALUE" [--type TYPE] [--project REF]
gobby memory recall [QUERY] [--project REF] [--limit N] \
  [--tags-all TAGS] [--tags-any TAGS] [--tags-none TAGS]
gobby memory list [--type TYPE] [--project REF] [--limit N] \
  [--tags-all TAGS] [--tags-any TAGS] [--tags-none TAGS]
```

`TAGS` is a comma-separated list.

### Inspect and Update

```bash
gobby memory show MEMORY_ID_OR_PREFIX [--project REF]
gobby memory update MEMORY_ID_OR_PREFIX [--content "NEW CONTENT"] [--tags "tag1,tag2"] [--project REF]
gobby memory delete MEMORY_ID_OR_PREFIX [--project REF]
gobby memory stats [--project REF]
```

Memory references can be full UUIDs or unambiguous prefixes.

### Markdown Export

```bash
gobby memory export [--project REF] [--output FILE] [--no-metadata] [--no-stats]
```

This writes a human-readable Markdown report. It is separate from the JSONL
backup path.

### Backup and Restore

```bash
gobby memory backup [--output PATH] [--quiet]
gobby memory restore [--input PATH] [--quiet]
```

The default JSONL path is
`~/.gobby/backups/<project-uuid>/memories.jsonl`. Backup is a machine-local
filesystem export for disaster recovery or migration. The PostgreSQL hub
remains the source of truth.

### Maintenance

```bash
gobby memory dedupe [--dry-run] [--yes]
gobby memory backfill-unscoped-lessons [--project REF]
gobby memory graph-counts [--project REF] [--json]
gobby memory reindex-embeddings
gobby memory reconcile [--dry-run]
gobby memory rebuild-crossrefs [--project REF]
gobby memory clear-graph [--project REF] [--yes]
gobby memory rebuild-graph [--project REF] [--wait] [--timeout SECONDS]
gobby memory invalidate [--project REF] [--yes]
```

Daemon-backed commands require the Gobby daemon because they call HTTP routes
for vector, graph, and index maintenance.

These are operator surfaces. Agents use the MCP memory tools. `dedupe` groups
trimmed identical content within each project, keeps the earliest row, and
hard-deletes later copies after confirmation (or `--yes`). Preview with
`--dry-run`. `backfill-unscoped-lessons` stamps scope metadata on eligible review
lessons; it does not transfer project ownership. `invalidate` rebuilds secondary
indices rather than deleting authoritative hub memories.

Recall telemetry maintenance lives under `gobby memory recall-signals`:
`backfill-events`, `backfill-labels`, `gate`, `audit-labels`,
`supersede-legacy-cohort`, `drift`, and `replay-candidate-filter`. Use each
subcommand's `--help` for required input paths, cohort selectors, and output
options. Backfills and cohort supersession mutate telemetry. Filter by caller,
label provenance, and dates when evaluating results; historical automatic recall
(`memory.recall`) and rule-driven surfacing (`memory.surface`) are separate
cohorts from explicit agent search.

## MCP Tools

Access memory tools through the `gobby-memory` server. Use `get_tool_schema`
for the authoritative signature before calling a tool.

| Tool | Purpose |
| --- | --- |
| `create_memory` | Store a memory. Requires `content` and `rationale`; accepts optional `memory_type`, `tags`, `supersedes`, and `session_id`. Returns the top five search results for the new content (`similar_existing`, undecayed score) and auto-supersedes any at raw cosine >= 0.9. |
| `search_memories` | Hybrid search with `query`, `limit`, `min_score` (undecayed axis), and tag filters. Hits carry `rationale`, `similarity`, `raw_semantic_score`, `undecayed_similarity`, provenance, and `collapsed_duplicates`; `diagnostics` reports candidates and the score range. |
| `list_memories` | List project-scoped memories with optional `memory_type`, `limit`, and tag filters. |
| `get_memory` | Read one memory by ID. |
| `update_memory` | Update `content`, `tags`, `rationale`, or `memory_type` for one memory. A content change requires a fresh `rationale`; content and rationale edits re-embed the vector. |
| `delete_memory` | Hard-delete one memory by ID (unrecoverable). Prefer `create_memory(..., supersedes=[id])` when a replacement exists. |
| `restore_memory` | Restore an owned soft-hidden row and its indices; cannot undo hard deletion. |
| `promote_memory_to_global` | Expose an owned memory globally without removing its owner. |
| `demote_memory_from_global` | Restrict visibility to the owning project. |
| `move_memory` | Transfer ownership to a concrete project. |
| `recluster_knowledge_graph_entities` | Offline HDBSCAN entity clustering; defaults to caller project. |
| `densify_knowledge_graph_cooccurrence` | Materialize derived co-occurrence support edges; defaults to caller project. |
| `get_related_memories` | Return cross-reference neighbors for one memory. |
| `memory_stats` | Return counts and summary stats. |
| `search_knowledge_graph` | Search extracted FalkorDB memory entities. |
| `rebuild_crossrefs` | Rebuild memory-to-memory cross-reference edges. |
| `rebuild_knowledge_graph` | Extract entities and relationships into FalkorDB. |
| `reindex_embeddings` | Regenerate embedding vectors for stored memories. |
| `review_task_memories` | Search memories related to a task after it closes and record that closure's memory review. |
| `surface_memories` | Search memories related to a block of text and return the ranked hits as a compact index; review lessons excluded, fails open to an empty result. |
| `restore_memories` | Restore the project memory backup into the hub database without deleting absent or newer rows. |
| `backup_memories` | Back up current live project memories to the machine-local project backup path. |
| `memory_dream` | Review stale memories, apply a validated plan, and snapshot mutations. |
| `memory_dream_status` | Return status and summary for a memory dream run. |
| `memory_dream_decisions` | Page proposed and effective actions, outcomes, and historical snapshots by run ID. |
| `memory_dream_revert` | Revert a memory dream run from its snapshots. |
| `judge_shadow_relevance` | System lifecycle tool for independent turn-end shadow-relevance judging. |

MCP content updates require a fresh rationale. The operator CLI update
exposes content/tags; HTTP also accepts memory type. Both preserve existing rationale. This is a
surface distinction, not an agent escape from the MCP capture contract. Content
is capped at 3,000 characters; rationale at 500; overlong writes are rejected.

`rebuild_crossrefs` and `rebuild_knowledge_graph` accept an explicit project and
default to at most 500 memories. Omitted project is not automatically the caller
scope for these tools. `reindex_embeddings` has no MCP project selector. Inspect
the result counts and per-operation errors before claiming complete repair.

When a memory call exceeds the inline MCP result budget, the proxy returns an
offload envelope containing a `result_id`. Page the raw result with
`gobby-results:get_tool_result`, or search its stored chunks with
`gobby-results:search_tool_result`. The envelope is a successful result; do not
repeat the original memory call merely to make its output smaller.

### Common Calls

```python
call_tool(server_name="gobby-memory", tool_name="list_memories", arguments={
    "memory_type": "preference",
    "limit": 20,
    "tags_none": ["stale"]
})
```

```python
call_tool(server_name="gobby-memory", tool_name="update_memory", arguments={
    "memory_id": "MEMORY_UUID_OR_PREFIX",
    "content": "Use task-linked commits for Gobby work.",
    "rationale": "Closing a leaf requires a linked commit; sessions re-derive this every week.",
    "tags": ["workflow", "commits"]
})
```

```python
call_tool(server_name="gobby-memory", tool_name="memory_dream", arguments={
    "dry_run": True,
    "memory_type": "fact"
})
# Returns a run ID immediately. Read status for bounded diagnostics; see Dream Operations.
```

## Post-Task Review

After a worked leaf closes, call `review_task_memories` with the task reference,
a concrete changes summary, and caller identity through outer `session_id`.
It requires a task closed by that session or one of its spawned descendants in
the caller project. It searches project/global candidates and records the closure
review; the caller still evaluates each candidate and updates or deletes stale
knowledge. Zero candidates is a valid complete review. Most tasks need no new
memory. Use the returned canonical `source_task_id` if capture is justified.

`pending_reviews_complete` and `pending_reviews` describe outstanding closures.
The closure queue can retain already-reviewed entries until a later close cleans
them up; do not repeatedly review just to empty a stored variable. The one-shot
memory review acknowledgement is separate from the feedback context-epoch latch.

## Review Lessons

The `gobby-review-learning` server exposes `recall_review_context`,
`recall_review_lessons_for_files`, `recall_review_lessons_by_class`,
`list_check_keys`, `record_review_lesson`, and `retire_review_lesson`.
Before final triage, recall findings and proposed fixes and include a Relevant
memory/lesson column in the finding table. Prefer established project knowledge
unless current code disproves it. Sweep sibling code for the same pattern before
declaring the fix complete.

Record only verified reusable outcomes. `confirmed` and `no-fix-policy` require
a finding title/message and principle/prevention. `stale` and `invalid` are
no-ops. Prefer `pattern_id` and `root_cause`; optional `query_hints`,
`lesson_type`, fingerprint, guardrail target, rule identity, severity, path/line,
symbol, and suggestion describe the diagnostic. `diagnostic_format` can be
`raw`, `sarif`, `rdjson`, or `review_comment`. CI, static-analysis, and
test-failure lessons require a verified fix reference in evidence. Include the
commit, changed files, validation command, review link, or no-fix rationale that
actually proves the outcome. A guardrail target is metadata and causes no task
or repository mutation. Retire an injected obsolete pattern only with nonempty
verification evidence and caller identity.

Plan-domain lessons use closed classes `reviewer-miss` and
`fixer-induced-defect`. Call `list_check_keys` for the target plan class before
minting a key; reuse an existing equivalent check. Identity is
`plan-review:<lesson_type>:<adversary-category>:<check_key>`, with explicit
`check_key`, `guardrail_target=checklist`, and
`rule_id=plan-review:<adversary-category>`. Omit a plan-file path.

| Adversary category | Starter check key |
| --- | --- |
| `missing-requirement` | `requirement-coverage` |
| `bad-sequencing` | `dependency-order` |
| `unhandled-edge` | `edge-case-coverage` |
| `weak-testability` | `acceptance-observability` |
| `traceability` | `requirement-traceability` |
| `over-engineering` | `proportionality` |
| `gobby-format` | `plan-contract` |

Reviewer-miss proof supplies all `participating_section_ids`, the earlier and
approval evidence IDs, and missed completed-round count; every participating
section must remain hash-unchanged. Fixer-induced-defect proof supplies all
`causal_section_ids`, causal finding, introduced round, and causal/approval
evidence IDs; every causal section must have changed. Evidence services compare
immutable manifests. Unknown IDs, empty required sets, or partial proof fail.
Dual-class lessons need both independent bundles, while one proven class remains
recordable if the other is unproven. The reviser records after final approval is
checkpointed; the adversary supplies attestations and never records its own round.

## Dream Operations

`memory_dream` returns a run ID immediately. `dry_run` defaults false; set true
for a report-only preview of memory actions. Dry runs still record diagnostic
run state. `full_sweep` broadens candidate selection. With caller project context
MCP starts a scoped run; without it the coordinator starts all due project scopes,
each with its own truth digest. Equivalent active work may coalesce; conflicting
work is rejected. Do not start another run to observe the first.

Use `memory_dream_status` for checkpoints, outcome, and publication summary.
`memory_dream_decisions` returns proposed versus effective actions and outcomes;
follow `next_offset` until null for complete review (limit 1–100). Older runs can
report `historical_rationale_missing` and fall back to snapshots. Do not infer a
proposal was applied. Inspect snapshots and retention before `memory_dream_revert`.
Revert reports conflicts separately from success and can defer secondary-store
reconciliation. Inspect `conflicts` and `secondary_sync_failures`; snapshot errors
produce `revert_failed`, and forfeited snapshots cannot be restored. A successful
envelope does not prove every action-owned column was reverted.

There is no dedicated Dream MCP wait tool. Agents must preserve the event-driven
wait contract: use status for
bounded diagnostics, do independent work, or yield; do not run an unbounded
polling loop or invent an automatic wake. Operators can use:

```bash
gobby memory dream --dry-run [--full] [--timeout SECONDS]
gobby memory dream status RUN_ID
gobby memory dream revert RUN_ID
```

The CLI observer polls internally. Its timeout or interruption stops only client
observation; the daemon run continues. Recover unavailable dependencies from the
durable checkpoint before retrying. Coordinate stop/restart with active sessions
and honor restart-protected dream cron work.

## HTTP Routes

The daemon exposes standard memory routes under `/api/memories`. Memory dream
routes are top-level daemon routes under `/memory/dream`; they are not prefixed
with `/api/memories`.

| Method and route | Purpose |
| --- | --- |
| `GET /api/memories` | List memories with `project_id`, `memory_type`, `limit`, and `offset`. |
| `POST /api/memories` | Create with required `content` and `rationale`; supports type, scope, provenance, tags, and supersession. |
| `GET /api/memories/search` | Search memories with required query parameter `q`, plus `project_id` and `limit`. |
| `GET /api/memories/stats` | Return memory counts, optionally scoped by `project_id`. |
| `POST /memory/dream` | Start an asynchronous memory dream run; returns the run ID immediately (202 admitted, 200 coalesced, 409 conflicting active run). |
| `GET /memory/dream/{run_id}` | Return dream run status, durable checkpoint, and summary. |
| `POST /memory/dream/{run_id}/revert` | Revert a dream run from snapshots. |
| `GET /api/memories/{memory_id}` | Read one memory, optionally scoped by `project_id`. |
| `PUT /api/memories/{memory_id}` | Update memory `content`, `tags`, or `memory_type`. |
| `DELETE /api/memories/{memory_id}` | Delete one memory. |
| `POST /api/memories/{memory_id}/restore` | Restore a soft-hidden memory. |
| `POST /api/memories/{memory_id}/promote` | Set global visibility for an owned memory. |
| `POST /api/memories/{memory_id}/demote` | Restrict an owned memory to its project. |
| `POST /api/memories/{memory_id}/move` | Move ownership to another project. |
| `GET /api/memories/graph/counts` | Return actual graph counts. |
| `GET /api/memories/graph` | Return recent memories and cross-reference edges for graph views. |
| `GET /api/memories/graph/entities` | Search extracted knowledge-graph entities. |
| `GET /api/memories/graph/entities/{entity_key}/neighbors` | Return entity neighbors. |
| `POST /api/memories/crossrefs/rebuild` | Rebuild memory cross-references. |
| `POST /api/memories/graph/clear` | Clear the FalkorDB memory graph projection. |
| `POST /api/memories/graph/rebuild` | Rebuild the knowledge graph, optionally in the background. |
| `GET /api/memories/graph/rebuild/status` | Inspect background rebuild status. |
| `POST /api/memories/embeddings/reindex` | Regenerate embedding vectors. |
| `POST /api/memories/reconcile` | Reconcile Qdrant and FalkorDB with the hub database. |
| `POST /api/memories/invalidate` | Clear secondary indices and start a background rebuild. |

## Architecture

The PostgreSQL hub is the source of truth. Runtime connection details come from
the `database_url` in `~/.gobby/bootstrap.yaml`.

```mermaid
flowchart LR
    Agent[Agent or CLI] --> MCP[gobby-memory MCP]
    Agent --> CLI[gobby memory CLI]
    MCP --> Manager[MemoryManager]
    CLI --> Manager
    HTTP[/api/memories] --> Manager
    Manager --> Hub[(PostgreSQL hub)]
    Manager --> BM25[pg_search BM25]
    Manager --> Qdrant[Qdrant vectors]
    Manager --> FalkorDB[FalkorDB knowledge graph]
    Manager --> JSONL[~/.gobby/backups/project-uuid/memories.jsonl]
```

`MemoryManager` coordinates storage, keyword search, vector search,
cross-references, image ingestion, cleanup, and the optional knowledge graph.
`StorageAdapter` provides the async backend interface over the hub storage layer.

### Search

Search uses the best available local infrastructure:

1. With Qdrant and embeddings configured, the query is embedded exactly as
   written and matched against memory vectors. Write the query as a sentence
   that describes the need; nothing rewrites or shortens it.
2. If FalkorDB graph search is available, graph matches join vector and keyword
   results through reciprocal-rank fusion.
3. pg_search BM25 keyword search participates when semantic search is available
   and is the fallback when vectors are unavailable.
4. Result metadata can include `similarity`, `search_via`, `ranking_score`,
   `raw_semantic_score`, `temporal_decay_factor`, and `ranking_mode`.

Results are ordered by interleaving two rankings of the candidate pool,
similarity first: undecayed similarity, and the fused score (`ranking_score`).
Each hit keeps the earlier of its two slots, so the best semantic match stays
first, the hit the searches most agree on comes second, and a memory that
several searches confirm can outrank one with higher similarity. Temporal decay
only breaks ties between otherwise equal hits. Hits no search could score come
last. A graded cohort of 24 queries
(`tests/memory/fixtures/ranking_cohort.json`) selected this order over
similarity alone, the fused score alone, and three weighted blends;
`tests/memory/test_ranking_policy.py` holds the selection rule and fails if the
shipped order is not its winner. The fused score depends on `limit`, because
each search fetches a window proportional to it, so the same memory can rank
differently at different limits.

`search_memories` supports an explicit `min_score` threshold. Agents search on
demand; `surface_memories` is the automated path, called by rules. The tool
returns
`memories`, `recall_request_id`, `project_id`, and `diagnostics`; each hit
includes its content, rationale, type, provenance, ranking fields, and duplicate
fold information. Live-corpus raw cosine score bands are p10 `0.62`, p50
`0.69`, and p90 `0.75`. Compare hits within the returned set and judge their
content and rationale instead of treating one score as a universal relevance
boundary. `min_score` filters the reported `undecayed_similarity` axis;
`similarity` includes temporal decay.

### Knowledge Graph

The knowledge graph extracts entities and relationships from memories into
FalkorDB. It is optional and depends on an LLM service, embeddings, a vector
store, and FalkorDB. Use it for relationship exploration, graph visualization, and
entity-oriented recall. Hub memories remain authoritative.

## Configuration

Memory-specific settings live under `memory:`. Shared vector and graph
connection settings live under `databases:` and shared embedding settings live
under `embeddings:`.

```yaml
memory:
  enabled: true
  backend: local
  auto_crossref: false
  crossref_threshold: 0.3
  crossref_max_links: 5
  access_debounce_seconds: 60
  temporal_decay_half_life_days: 30.0
  code_link_min_score: 0.82
  kg:
    profile: feature_low
    candidates: []
  dream:
    enabled: true
    schedule_cron: "0 2 * * *"
    prompt_path: memory/dream
    max_tokens: 8192
    planner_batch_size: 25
    max_runtime_seconds: 14400
    work_unit_timeout_seconds: 1500.0
    evidence_channel_timeout_seconds: 30.0
    evidence_retry_attempts: 3
    evidence_phase_timeout_seconds: 210.0
    min_action_confidence: 0.72
    min_delete_confidence: 0.85
    include_global_memories: true
    reconcile_after_apply: true

embeddings:
  model: nomic-embed-text
  dim: 768
  api_base: null
  api_key: null

databases:
  qdrant:
    url: http://localhost:6333
    port: 6333
    collection_prefix: code_symbols_
  falkordb:
    host: 127.0.0.1
    port: 16379
    password: ${GOBBY_FALKORDB_PASSWORD:-}
    graph_name: gobby_kg
    graph_search: true
    graph_min_score: 0.5
    rrf_k: 60

memory_backup:
  enabled: true
```

Knowledge-graph extraction is enabled by FalkorDB being configured
(`databases.falkordb.password`); `memory.kg` only selects the LLM profile and
candidates for extraction — there is no `kg.enabled` flag.

`memory_backup` configures the backup manager. With `backup_path` omitted, the
manager uses `~/.gobby/backups/<project-uuid>/memories.jsonl`; setting
`backup_path` selects an explicit override. Treat the file as a backup and
migration artifact, not a live bidirectional source of truth.

Automatic prompt recall configuration has been removed. Legacy
`memory_recall` and `memory.min_recall_score` settings fail configuration
validation; agents choose a per-call `search_memories(min_score=...)` threshold
when a task needs one.

## Lifecycle Rules

Memory lifecycle automation is installed as rules against semantic workflow
events.

```mermaid
sequenceDiagram
    participant User
    participant RuleEngine
    participant Memory as gobby-memory
    participant Agent

    User->>RuleEngine: turn_start(prompt)
    RuleEngine->>Memory: surface_memories(prompt or last assistant message)
    RuleEngine-->>Agent: ranked memory index, plus the memory skill in a new context epoch
    Agent->>Memory: search_memories(query) when the work needs prior knowledge
    Agent-->>User: response
    RuleEngine-->>Agent: post-close review request on turn_end or before set_handoff (when tasks closed)
    RuleEngine->>Memory: judge_shadow_relevance on turn_end
```

The installed `bootstrap-default-agent-core-skills` rule requests memory guidance
with the other core skills in each context epoch. It replaces the old separate
initial-turn loader. Installed registry inspection on 2026-09-12 found that
bootstrap rule and every rule then bundled enabled globally; the five
`surface-memories-*` rows are newer, so confirm them with `gobby rules list`
after the next template sync. Re-check installed rows for the current session
before inferring active enforcement from this table.

Current bundled memory rules:

| Rule | Event | Behavior |
| --- | --- | --- |
| `check-memory-guidance-on-initial-stop` | `turn_end` | Blocks the first turn end once until `gobby:references/memory/overview.md` is loaded or its fetch failed. |
| `review-closed-task-memories-before-handoff` | `before_tool` | Blocks `gobby-sessions:set_handoff` once per queued closure set, so a handoff right after `close_task` cannot defer the review past the closing context; silent once every queued closure is reviewed. |
| `review-closed-task-memories-on-stop` | `turn_end` | Blocks once per queued closure set with a `review_task_memories` request; silent once every queued closure is reviewed. |
| `judge-shadow-relevance-on-response` | `turn_end` | Judges pending shadow-memory recall candidates in the background. |
| `guard-plan-memory-writes` | `before_tool` | Blocks the first plan-time `create_memory` or `update_memory` call until the agent confirms that the write is a durable preference or finalized decision rather than plan evidence. |
| `reset-memory-tracking-on-start` | `session_start` | Clears injected review-lesson tracking after clear, compact, or selected resume events. |
| `increment-parent-turn-seq` | `turn_start` | Increments the parent session turn sequence counter. |
| `surface-memories-on-turn-start` | `turn_start` | Calls `surface_memories` once per parent turn and injects the ranked index, searching on the prompt when it states work and on the session's last assistant message when it does not. |
| `surface-memories-before-spawn` | `before_tool` | Surfaces a ranked memory index for the spawn prompt before `gobby-agents:spawn_agent` runs. |
| `surface-memories-before-claiming-create` | `before_tool` | Surfaces the index for a `create_task` title when the same call claims the task. |
| `surface-memories-after-claim` | `after_tool` | Surfaces the index for the claimed task's title after a successful `claim_task`. |
| `surface-memories-after-handoff` | `after_tool` | Surfaces the index for the handoff text `gobby-sessions:get_handoff` returned. |

Queueing a closed task for review is not one of these rules: the workflow state
manager writes `_memory_pending_task_reviews` directly, and the two review rows
above read it.

Author new lifecycle rules against semantic events such as `turn_start` and
`turn_end`. Raw provider/runtime hook names are transport details.

## Retrieval: Pushed Index and Agent Search

Memory reaches an agent two ways: a pushed index and the agent's own search.

Automatic surfacing is bounded to the five moments in the rule table above:
`surface-memories-on-turn-start` pushes one ranked memory index per parent turn,
and the four tool-intent rules push one at an agent spawn, a claiming
`create_task`, a successful `claim_task`, and a handoff read. Each index is a
`<memory-index trigger="...">` block of up to five hits, one line per hit: rank,
short ID, type, the searches that found it, its last-updated date, and the lead
of its content. A hit with a rationale ends in `| when:` and the rationale's
lead, so the line says when the memory applies. The index carries no scores;
the live score band is too narrow to read. Surfacing searches with caller
`memory.surface`, excludes review lessons, and injects nothing when the search
fails or finds no hit.

Fetch any hit whose `when:` clause matches the situation with `get_memory`
before acting, even when the code is familiar: what pays off is usually a prior
decision or an observed runtime behavior rather than code.

Everything beyond those indexes is the agent's own search. Call
`search_memories` after claiming unfamiliar work, before characterizing provider
or runtime behavior, before recording a finding, and whenever prior project
knowledge could change the implementation. Judge each hit by its `similarity`,
`type`, `rationale`, and content; search results are evidence, not authority.

The index prints each rationale as the `when:` clause, so write a rationale as
the situation in which a future session needs the memory. Most turns need no
memory write.

Rule-delivered review lessons and surfaced memory indexes are deduplicated for
one context epoch through
`injected_memory_ids`. Clear, compact, and selected resume events start a new
context epoch by resetting that variable, allowing relevant guidance to appear
again without suppressing it for the whole session.

## Backup Format

`~/.gobby/backups/<project-uuid>/memories.jsonl` stores one JSON object per line.
Backup writes exactly the current live scoped rows in deterministic order.
Restore validates the complete file before writing, upserts by memory ID and
updated timestamp, and preserves database-only and newer database rows.

```jsonl
{"id":"8de06cb8-99b8-4fc4-a16c-5af217132b81","type":"fact","content":"Use uv for local development","tags":["tooling"],"source":"agent","created_at":"2026-07-20T12:00:00Z","updated_at":"2026-07-20T12:00:00Z"}
{"id":"70d53c95-4316-4f79-a2c2-6e1e25781063","type":"preference","content":"Prefer focused validation over full suite runs","tags":["testing"],"source":"user","created_at":"2026-07-20T12:05:00Z","updated_at":"2026-07-20T12:05:00Z"}
```

Use `gobby memory backup` or MCP `backup_memories` to write the file. Use
`gobby memory restore` or MCP `restore_memories` to restore it explicitly.

## Maintenance Checklist

- Search before creating a memory to avoid duplicates.
- Delete stale memories when you discover them.
- Use `gobby memory dream --dry-run` or MCP `memory_dream` with `dry_run=true`
  for a report-only hygiene pass.
- Rebuild cross-references after large imports or cleanup.
- Reindex embeddings after changing embedding providers or models.
- Rebuild or clear the knowledge graph when entity extraction changes.
- Reconcile stores when Qdrant or FalkorDB may contain orphaned records.

## Troubleshooting

### A search returns nothing useful

Check that memory is enabled and the relevant memories are in the current
project scope, then widen the search:

```python
call_tool(server_name="gobby-memory", tool_name="search_memories", arguments={
    "query": "the missing context",
    "limit": 10,
    "min_score": 0.0
})
```

### Search quality is poor

Run a tag-filtered search to confirm the memory exists, then verify embeddings
and Qdrant are available. Without embeddings, Gobby falls back to keyword
search.

```bash
gobby memory recall "query words" --tags-any "architecture,workflow"
gobby memory reindex-embeddings
```

### Backup file is missing

Run an explicit backup:

```bash
gobby memory backup
```

If the file exists but restored memories do not appear, check project scope and
run `gobby memory restore` to read the current project's default backup.

### Graph views are empty

The knowledge graph is optional. Verify FalkorDB, embeddings, and an LLM provider
are configured, then rebuild:

```bash
gobby memory rebuild-graph --wait
```

## File Locations

| Path | Description |
| --- | --- |
| `~/.gobby/bootstrap.yaml` `database_url` | Runtime PostgreSQL hub DSN. |
| `~/.gobby/bootstrap.yaml` | Bootstrap settings, including Postgres install metadata. |
| `~/.gobby/backups/<project-uuid>/memories.jsonl` | Machine-local JSONL memory backup/export file. |
| `src/gobby/memory/` | Memory manager, search, graph, indexing, and maintenance code. |
| `src/gobby/mcp_proxy/tools/memory.py` | `gobby-memory` MCP tool definitions. |
| `src/gobby/cli/memory/` | CLI command package (crud, dream, export, graph, indices, maintenance). |
| `src/gobby/servers/routes/memory.py` | HTTP memory routes. |
| `src/gobby/install/shared/workflows/rules/memory-lifecycle/` | Bundled memory lifecycle rules. |

## Related Documentation

- [Tasks](./tasks.md) - Track actionable work instead of storing it as memory.
- [Sessions](./sessions.md) - Session transcripts, summaries, and handoffs.
- [MCP Tools](./mcp-tools.md) - Progressive discovery and internal MCP tool usage.
- [Workflow Rules](./workflow-rules.md) - Semantic lifecycle events and rule effects.

_Last verified: 2026-09-18_
