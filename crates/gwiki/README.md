# gwiki

`gwiki` is the Gobby wiki CLI. It ingests local files, indexes wiki content,
and can derive Markdown from media when AI routing is enabled.

AI configuration is shared with the other Gobby CLI crates through
`gobby_core`. The full capability matrix, local model guidance, and routing
examples live in [docs/guides/ai-configuration.md](../../docs/guides/ai-configuration.md).

For one command, use `gwiki ingest-file --no-ai` to keep media ingest on the
privacy path. Use `--transcription-routing`, `--vision-routing`, and
`--text-routing` to override configured AI routing for a single ingest.
