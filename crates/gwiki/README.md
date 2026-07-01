# gwiki

`gwiki` is the Gobby wiki CLI. It ingests local files, indexes wiki content,
and can derive Markdown from media when AI routing is enabled.

PostgreSQL-backed search uses ParadeDB `pg_search` BM25 indexes on
`gwiki_documents` and `gwiki_chunks`. Run `gwiki setup --standalone` only
against a PostgreSQL hub where the `pg_search` extension is installed; setup
preflights the extension before creating BM25 indexes.

AI configuration is shared with the other Gobby CLI crates through
`gobby_core`. The full capability matrix, local model guidance, and routing
examples live in [docs/guides/ai-configuration.md](../../docs/guides/ai-configuration.md).

For one command, use `gwiki ingest-file --no-ai` to keep media ingest on the
privacy path. Use `--transcription-routing`, `--vision-routing`, and
`--text-routing` to override configured AI routing for a single ingest.

## Retrieval

`gwiki search` is the retrieval primitive for humans and agents: hybrid
BM25 + semantic + graph-boosted hits with bounded query-token snippets,
provenance (`wiki_page`, `source_path`, `result_type`, `sources`), and
`code_citations` tied to the returned hits. Full document bodies never appear
in search output; follow a hit with `gwiki read --path <wiki_page>`. Agents
compose `search` + `read` for research loops and deposit results back through
`collect`/`ingest-file`.

`gwiki ask` is a thin RAG layer over the same retrieval: top-k hits become a
bounded evidence prompt (~12K-token cap, reported via `prompt_token_budget` /
`prompt_tokens_estimated`), one completion runs through the daemon route or a
direct OpenAI-compatible endpoint (`--ai daemon|direct|auto`, including
LM Studio), and the answer is checked against the evidence with grounded
citations.

## Source Refresh

`gwiki index` rebuilds derived search state from files already on disk.
`gwiki refresh` updates existing source records first, then indexes once when a
batch changed.

```bash
gwiki refresh [--id SOURCE_ID ...] [--dry-run] [--format json] [--project | --topic NAME]
```

Refresh is manifest-driven from `raw/INDEX.md`. It refreshes URL records and
local files originally captured by `gwiki ingest-file`, including audio, image,
video, PDF, Office, HTML, Markdown, text, and generic file sources. It never
scans directories or follows globs. Unchanged local file bytes are reported
without rerunning AI/media extraction. Connector records without replay
contracts, such as stdin, research notes, MediaWiki, Wayback, and Git
repositories, are skipped unless explicitly selected. Local file/media records
without replay metadata are reported in `failed`.
`--dry-run` reports candidates without fetching, reading source bytes, writing,
deleting, or indexing.

JSON output includes `command: "refresh"`, `scope`, `status`, `dry_run`,
`planned`, `refreshed`, `unchanged`, `failed`, `skipped`, `indexed`,
`index_status`, and `degradations`. Changed content gets a new content-hash
source ID and reports `old_id`, `new_id`, `raw_path`, and `previous_raw_path`;
unchanged content is left untouched and `index_status.status` is `not_run`.
