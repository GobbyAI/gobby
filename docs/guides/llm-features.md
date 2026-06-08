# LLM Feature Routing

Gobby routes text-generation features through provider/model candidates. Keep config keys in
their external form; for task features that is `gobby-tasks`, matching the MCP server name.

## Profile Candidates

| Profile | Default candidates |
| --- | --- |
| `feature_low` | `codex/gpt-5.3-codex-spark`, `codex/gpt-5.4-mini`, `claude/haiku`, `local/Qwen3-Coder-30B-A3B-Instruct` |
| `feature_mid` | `codex/gpt-5.3-codex-spark`, `claude/sonnet`, `local/Qwen3-Coder-Next` |
| `feature_high` | `codex/gpt-5.3-codex`, `claude/opus`, `local/Qwen3-Coder-Next` |

## `call_feature` Features

| Feature | Config path | Default profile | Call path | Caller/use |
| --- | --- | --- | --- | --- |
| `session_summary` | `session_summary` | `feature_low` | `LLMService.call_feature(config.session_summary, ...)` | `sessions.summary`: session handoff summaries |
| `digest` | `digest` | `feature_low` | `LLMService.call_feature(config.digest, ...)` | `memory.turn_record`, `memory.title_synthesis`: rolling digest and title synthesis |
| `memory_recall` | `memory_recall` | `feature_low` | `FeatureDefaultConfig` used by daemon recall runner | Daemon-owned memory recall selection |
| `memory.kg` | `memory.kg` | `feature_low` | Knowledge graph extractor `_generate_json(...)` | `memory.kg.extract_entities`, `memory.kg.extract_relationships`, `memory.kg.select_outdated_relations` |
| `memory.dream` | `memory.dream` | `feature_mid` | `LLMService.call_json_feature(config.memory.dream, ...)` | `memory.dream`: validated memory maintenance planning |
| `tool_summarizer` | `tool_summarizer` | `feature_low` | `LLMService.call_feature(config.tool_summarizer, ...)` | `tools.tool_summary`, `tools.server_description`: MCP tool/server summaries |
| `recommend_tools` | `recommend_tools` | `feature_mid` | `LLMService.call_feature(config.recommend_tools, ...)` | `mcp_proxy.recommendation.hybrid_rerank`, `mcp_proxy.recommendation.llm`: tool recommendations |
| `import_mcp_server` | `import_mcp_server` | `feature_low` | `LLMService.call_feature(config.import_mcp_server, ...)` | MCP server import synthesis |
| `pipelines.prompt_step` | `pipelines.prompt_step` | `feature_low` | `LLMService.call_feature(config.pipelines.prompt_step, ...)` | `workflows.pipeline.prompt_step`: pipeline prompt steps |
| `skill_description` | `skill_description` | `feature_low` | `LLMService.call_feature(config.skill_description, ...)` | `skills.github_collection.description`: skill description synthesis |
| `merge_resolution` | `merge_resolution` | `feature_mid` | `LLMService.call_feature(config.merge_resolution, ...)` | `worktrees.merge.resolve_hunks`, `worktrees.merge.resolve_full_file`: merge conflict resolution |
| `gobby-tasks.expansion` | `gobby-tasks.expansion` | `feature_high` | `LLMService.call_feature(config.gobby_tasks.expansion, ...)` | `tasks.expansion.compile`: task expansion compilation |
| `gobby-tasks.validation` | `gobby-tasks.validation` | `feature_mid` | `LLMService.call_feature(config.gobby_tasks.validation, ...)` | `tasks.validation`: task completion validation |
| `chat` | `chat` | `feature_high` | Text-generation service receives `config.chat` candidates | Chat session generation defaults |
| `code_index.summary_candidates` | `code_index.summary_candidates` | `feature_low` | `TextGenerationService.generate(...)` with `config.code_index.summary_candidates` | `code_index.symbol_summary`: symbol summaries |

## Outside `call_feature`

`code_index.symbol_summary` does not call `LLMService.call_feature`. `SymbolSummarizer`
passes `code_index.summary_profile` and `code_index.summary_candidates` directly to
`TextGenerationService.generate(...)`.

`vision_extract` is capability-registry routed. The `/api/llm/vision/extract` route builds
`VisionExtractService`, selects an `AICapability.VISION_EXTRACT` binding, and invokes the
provider adapter. It is not routed through low/mid/high feature profiles.

File and image ingestion belongs to gwiki. MCP `wiki_attach` / `wiki_ingest`, HTTP
`/api/wiki/attach`, and `GwikiGateway.ingest_file` route those files through the gwiki
gateway/CLI path rather than memory storage or feature-profile `call_feature` calls.
