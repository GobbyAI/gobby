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
