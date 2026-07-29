# CodeRabbit Fixes: Rust, CodeWiki, and Gwiki

Rust fixes across gcode, gcore, and gwiki.

Unresolved original findings: **57**

Original finding IDs: 316-324, 386-388, 436-450, 503-509, 586-589, 627-635, 681-682, 725-732

## Finding #316

In @crates/gcode/src/commands/codewiki/generation.rs around lines 49 - 53, Correct the aggregate generator documentation in the module containing the tool-loop aggregate generator so it only claims tool-loop production and hard-failure behavior for repo overview and architecture pages; remove curated navigation, concept, and narrative pages unless they are actually routed through tool_loop. Update the corresponding module comment in codewiki/mod.rs to match the implemented generation paths and fallback behavior.

## Finding #317

In @crates/gcode/src/commands/codewiki/mod.rs around lines 56 - 58, Update the tool-loop description near the module-level comments in codewiki to remove curated navigation, concept, and narrative pages from the claimed output. Keep the description limited to pages actually produced by the tool loop, including its frontmatter and failure behavior.

## Finding #318

In @crates/gcore/src/ai/generation/one_shot.rs around lines 187 - 201, The Direct-route path creates a new reqwest blocking Client for every generate_text_with_target call, preventing connection reuse. Update the surrounding generation flow to reuse a shared Client across calls, while preserving the existing request construction, authentication, timeout, retry, and response parsing behavior.

## Finding #319

In @crates/gcore/src/ai/generation/one_shot.rs around lines 14 - 44, Move the use declarations for ChatCompletionRequest, ChatMessage, ToolChoice, and build_request_body above budget_for_tier so all imports remain together; leave budget_for_tier and its behavior unchanged.

## Finding #320

In @crates/gcore/src/ai/generation/tests/profile.rs around lines 162 - 219, Add a test alongside profile_resolves_api_keys_from_recognized_provider_environment covering a recognized provider such as "openai" with a custom, non-default TEXT_GENERATE_API_BASE. Set OPENAI_API_KEY and assert resolve_direct_generation_target returns no API key for that custom base, preserving environment-key resolution only for approved provider/base combinations.

## Finding #321

In @crates/gcore/src/search.rs around lines 146 - 159, Refactor the unescaped quote counting in the surrounding search logic to use an explicit fold or loop instead of mutating quote_backslash_run inside the filter predicate. Preserve the existing handling of backslash runs and the balanced_quotes calculation, including treating quotes as unescaped only when preceded by an even-length backslash run.

## Finding #322

In @crates/gwiki/src/commands/generation_routes.rs around lines 58 - 61, Add a test alongside the existing generation-route policy tests named daemon_agentic_max_turns_matches_tool_loop_default. Assert that DAEMON_AGENTIC_MAX_TURNS equals ToolLoopLimits::default().max_turns, preserving the documented coupling between daemon and direct-route turn budgets.

## Finding #323

In @crates/gwiki/src/search/bm25.rs around lines 426 - 434, Update the skip message in postgres_test_database_url to mention both accepted environment variables, GWIKI_POSTGRES_TEST_DATABASE_URL and GCODE_POSTGRES_TEST_DATABASE_URL, so users know either can configure the test database.

## Finding #324

In @crates/gwiki/src/search/graph_boost.rs around lines 229 - 261, Unify the backward-link scoring used by the ranking logic in the shown function and MemoryWikiGraph::related_paths_with_options. Extract the shared resolved-link/outdegree calculation and backward-weight formula, or at minimum define and reuse a shared BACKWARD_LINK_WEIGHT constant, while preserving options.backward_link_weight behavior where configured so both backends cannot silently diverge.

## Finding #386

In @crates/gcode/src/commands/codewiki/build_parts/architecture.rs around lines 101 - 104, Update the observability aggregation in both affected paths to treat a missing generated observability turn count as zero, preserving the existing aggregate total from earlier successful generations. Keep the aggregate unset only when observability collection itself is not configured, and apply the change to the logic around the visible turns aggregation.

## Finding #387

In @crates/gwiki/src/commands/ask/deep.rs around lines 362 - 385, Update deep_citation_check so zero-citation answers use a dedicated status or warning representation rather than inserting “answer contains no wiki citations” into unsupported_claims. Adjust the corresponding record_synthesis handling to render that condition as a dedicated no-citations warning, while preserving normal unsupported-claim reporting for answers containing citations.

## Finding #388

In @crates/gwiki/src/commands/ask/deep.rs around lines 440 - 474, Update deep_citation_check and page_with_stem_exists so the vault directory is traversed at most once per deep_citation_check call: build a reusable case-insensitive page-stem index from the walk, then resolve each bare single-segment citation against that index instead of recursively scanning the vault per link. Preserve the existing markdown-file and recursive-directory matching behavior.

## Finding #436

In @crates/gcode/src/commands/codewiki/build_parts/curated_content.rs around lines 722 - 746, Refactor the DiagramOutcome handling around the emitted branch into a single match that records the CuratedFlow pass exactly once for each outcome path. Keep the emitted outcome flowing into the existing block-processing logic, and preserve the non-emitted containment fallback with its “pass 3 containment fallback” label, without relying on recorded_slots deduplication.

## Finding #437

In @crates/gcode/src/commands/codewiki/build_parts/modules.rs around lines 126 - 153, Precompute the loop-invariant component metadata and module-level dependency edge set once before the per-module rendering loop, then pass those results into render_module_dependency_mermaid and render_module_call_sequence. Update the renderers and their helpers so they reuse the precomputed indexes and only perform module/page-scoped filtering and bounding inside each iteration.

## Finding #438

In @crates/gcode/src/commands/codewiki/diagram_compose.rs around lines 61 - 74, Replace the `DiagramKind::LABELS` lookup table and `label()` search with an exhaustive `match` on `self`, returning the corresponding stable label for `ModuleDependency`, `ModuleCallSequence`, and `CuratedFlow`. Mirror the pattern used by `DiagramOutcome::label` so new enum variants require compiler-checked handling and no runtime `expect` remains.

## Finding #439

In @crates/gcode/src/commands/codewiki/generation.rs around lines 336 - 337, Update the diagram statistics flow around the early return in the scoped generation path and the unconditional run.rs sink.set_diagram_stats call so scoped or reused runs cannot overwrite whole-vault telemetry with partial results. Preserve existing stats by merging with the previous metadata, or record an explicit scope/partial marker that prevents consumers from treating the result as full-vault statistics.

## Finding #440

In @crates/gcode/src/commands/codewiki/mod.rs around lines 231 - 238, Move the comment “Rendered markdown and graph-derived narrative analysis.” from above the compare re-exports to directly above the render re-export group beginning with pub(crate) use render, leaving the compare exports unchanged.

## Finding #441

In @crates/gcode/src/commands/codewiki/render/diagrams.rs around lines 164 - 187, Optimize the cyclic-page fallback by building a single adjacency map from all_edges in render_module_call_sequence, then update directed_call_distances to traverse only each dequeued node’s outgoing neighbors instead of rescanning all edges. Pass this index through both the root_seeds traversal and the in_page_components fallback while preserving the existing depth limit and SparseEvidence behavior.

## Finding #442

In @crates/gcode/src/commands/codewiki/render/diagrams.rs around lines 297 - 299, Update the caption string appended in the diagram rendering flow to remove the stray ellipsis after “call depth,” while preserving the surrounding wording and punctuation.

## Finding #443

In @crates/gcode/src/commands/codewiki/render/diagrams.rs around lines 475 - 490, Update mermaid_label to retain the existing "repo" fallback for empty modules, but delegate non-empty module escaping to gobby_core::vault::mermaid::escape_label instead of maintaining the local replacement chain.

## Finding #444

In @crates/gcode/src/commands/codewiki/run.rs around lines 592 - 620, Update capture_commit_stamp to determine dirty state using only tracked modifications, replacing the current git status --porcelain invocation with an appropriate tracked-files status check. Preserve the existing failure handling and CommitStamp construction, while ensuring untracked files do not set dirty and are not scanned.

## Finding #445

In @crates/gcode/src/commands/codewiki/run.rs around lines 436 - 467, Harden capture_commit_stamp_detects_dirty_worktrees_and_non_git_roots against ambient Git configuration by pinning the repository to SHA-1 object format and disabling commit signing in the relevant git invocations. Replace the fixed clean.sha length assertion with a shape assertion that accepts the configured SHA-1 format without relying on global settings.

## Finding #446

In @crates/gcode/src/commands/codewiki/tests/incremental.rs around lines 834 - 1102, Add a test alongside compare_to_distinguishes_bad_ref_and_invalid_baseline_metadata that invokes compare_to with an out value containing path traversal, such as "../escape", and asserts it fails with "requires --out to be inside the source repository". Reuse the existing committed_compare_repo setup and metadata fixtures.

## Finding #447

In @crates/gcode/src/commands/codewiki/tests/reuse.rs around lines 2351 - 2357, Extend the normalization refresh test around third.persist and third.finish to assert that the persisted metadata entry meta.docs[doc.path] still contains the original commit and generated timestamp. Keep the existing byte-for-byte page assertion, and compare against the original metadata captured before the refresh rather than the third sink’s values.

## Finding #448

In @crates/gwiki/src/health.rs around lines 909 - 953, Refactor duplicate_concepts to bucket pages by normalized exact-title and shared-key values, emitting one grouped DuplicateConcept per duplicate group with all matching paths instead of pairwise entries. Retain distinct_pairs filtering when forming groups, then restrict title_prefix detection to sorted-title adjacent candidates rather than scanning every concept pair, preserving the existing reason and title formatting semantics.

## Finding #449

In @crates/gwiki/src/links.rs around lines 134 - 166, Extend concept_worthiness_accepts_technical_terms_and_rejects_artifacts with a path-shaped key case, asserting is_concept_worthy returns false and concept_rejection_reason returns Some("path_shaped"). Use a representative path-like value that exercises the path_shaped branch in the existing rejection logic.

## Finding #450

In @crates/gwiki/src/upkeep.rs around lines 1071 - 1075, Update run_with_clock and find_unworthy_concepts to reuse the already-collected page list instead of calling lint::collect_pages(vault_root) again. Pass the existing pages (preferably as a slice) into find_unworthy_concepts and iterate them while preserving the current filtering and archive behavior.

## Finding #503

In @crates/gcode/src/commands/codewiki/compare.rs around lines 169 - 188, Update normalize_explicit_git_meta to normalize backslash separators before iterating path components and performing traversal validation, so "..\\_meta/codewiki.json" is rejected rather than converted after validation. Extend the invalid-path tests for normalize_explicit_git_meta to cover this Windows-style traversal input.

## Finding #504

In @crates/gcode/src/config/context.rs around lines 314 - 333, Extract the duplicated falkordb, qdrant, embedding, indexing, and code-vector resolution logic into a shared resolve_services(&mut conn, &layers, services, quiet) helper returning the existing tuple. Replace both resolver blocks with calls to this helper, preserving each service’s conditional behavior and error propagation.

## Finding #505

In @crates/gcode/src/config/layers.rs around lines 139 - 172, The test daemon_service_source_orders_env_served_and_routing must also verify the routing fallback tier. Add a ServiceSource::daemon case with a served map that omits databases.qdrant.url and the existing routing config, then assert config_value returns <http://routing.example:6333>.

## Finding #506

In @crates/gcode/src/config/services.rs around lines 380 - 385, Remove the unused _quiet parameter from resolve_falkordb_config, resolve_qdrant_config, and resolve_embedding_config, then update every caller in context.rs and elsewhere to stop passing quiet. Preserve each resolver’s existing behavior and avoid adding compatibility shims.

## Finding #507

In @crates/gcode/src/vector/code_symbols/embedding.rs around lines 148 - 174, Consolidate the duplicated AI source resolution failure handling in resolve_embedding_ai_context: extract or reuse a small helper for logging “failed to resolve effective AI config” and returning None, then apply it to both ai_source_for_conn and ai_source_without_primary error branches while preserving their existing success paths.

## Finding #508

In @crates/gcode/tests/effective_config.rs around lines 58 - 84, Update spawn_effective_config_server to apply a finite read timeout to the accepted TcpStream before the request-reading loop, so stalled or malformed requests fail promptly instead of hanging CI. Preserve the existing request parsing and response behavior for valid requests.

## Finding #509

In @crates/gcore/src/ai/effective_config.rs around lines 86 - 114, Extract the duplicated local-token bearer-header setup into a helper such as local_token::apply_bearer_header(request), reusing read_local_cli_token, AUTHORIZATION_HEADER, and authorization_bearer. Update the effective-config request flow and the corresponding request construction in probe.rs to call this helper, preserving the behavior of leaving requests unchanged when no local token is available.

## Finding #586

In @crates/gwiki/src/commands/read.rs around lines 125 - 131, Add a read_title_with_max_bytes delegator alongside read_title, accepting an explicit byte limit and passing it through the title-reading flow to read_existing_path; update read_title to resolve configured_read_max_bytes() and delegate to it, keeping title reads symmetric with read_path and independently testable.

## Finding #587

In @crates/gwiki/src/commands/search.rs around lines 128 - 146, Consolidate the repeated ai_source_for_conn calls in the embedding, qdrant, and falkor configuration blocks into one shared mutable source initialized once with the existing WikiError::Config mapping. Reuse that source sequentially for resolve_semantic_embedding, resolve_qdrant_config, and resolve_falkordb_config, preserving each resolver’s current behavior and error context.

## Finding #588

In @crates/gwiki/src/support/config.rs around lines 153 - 185, The two layer-resolution helpers duplicate layer selection and source construction; replace them with one generic helper that accepts a terminal resolver callback and returns its generic result. Update resolve_index_options_from_layers and resolve_shared_code_graph_limits_from_layers to delegate to this helper while preserving their existing resolver calls and standalone-loading behavior.

## Finding #589

In @crates/gwiki/src/support/test_env.rs around lines 51 - 64, Update daemon_config_disabled() and its callers so reads of DAEMON_CONFIG_DISABLE_ENV are serialized with ENV_TEST_LOCK, or move the override behind process isolation; ensure every var_os() read is protected against concurrent EnvGuard mutations while preserving existing configuration behavior.

## Finding #627

In @crates/gcode/tests/graph_standalone/support.rs at line 83, Replace the literal "GOBBY_RUNTIME_MODE" in the test environment setup with gobby_core::runtime_mode::RUNTIME_MODE_ENV, and apply the same constant wherever this environment variable is referenced in the related standalone and stale projection tests.

## Finding #628

In @crates/gcore/src/runtime_mode.rs around lines 55 - 73, Change parse_requested_mode and select_runtime_mode_with_probe so the parser returns a standalone-specific result type, such as an existing or new StandaloneOverride, rather than RuntimeMode; update the match to handle only the standalone override and absence, removing the Some(RuntimeMode::Daemon) unreachable arm while preserving the existing daemon URL, service registration, and standalone fallback behavior.

## Finding #629

In @crates/gwiki/src/commands/index.rs around lines 238 - 241, Clamp the max_age_hours value at the IngestUrl CLI argument boundary in cli.rs to the inclusive range 0..=8760, ensuring direct gwiki ingest-url invocations cannot request unbounded cache reuse. Locate the IngestUrl argument definition and apply the same bound used by the gateway before execute_ingest_url receives the value.

## Finding #630

In @crates/gwiki/src/health/tests.rs around lines 1 - 21, Move the source_reference_is_present helper below the complete use/import block, keeping its implementation unchanged and preserving the existing imported symbols from aho_corasick and the citations module.

## Finding #631

In @crates/gwiki/src/ingest/url/tests.rs around lines 305 - 345, Add assertions in within_ttl_uses_manifest_cache_without_fetch_or_store for the cached-only result’s UrlBatchIngest status() and exit_code(), expecting "ingested" and 0. Add focused coverage for a cached-plus-failed batch, asserting status() is "partial" and exit_code() is 0, using the existing batch/result construction patterns and symbols.

## Finding #632

In @crates/gwiki/src/ingest/url/tests.rs around lines 432 - 523, Use separate MemoryWikiStore instances for the HTML and PDF ingestion flows in missing_url_artifacts_and_invalid_freshness_refetch_for_self_healing: create an HTML-specific store for the first ingest and a PDF-specific store for all PDF ingests, rather than reusing store across distinct vault roots.

## Finding #633

In @crates/gwiki/src/upkeep/runner.rs at line 36, Document the purpose of the MIN_CLUSTER_REMAINING_SECONDS constant, noting that its approximately 20-minute reservation preserves tail execution budget and causes runs with less remaining time to defer every cluster on the first pass.

## Finding #634

In @crates/gwiki/src/upkeep/tests.rs around lines 1087 - 1109, The time-budget test around run_with_clock should document the intentional relationship between the 1320-second budget and the 111-second clock jump after the second call, including the per-cluster projection ratio being exercised. Add a concise comment near these magic values without changing the test behavior.

## Finding #635

In @crates/gwiki/src/upkeep/tests.rs around lines 683 - 687, Replace the vacuous disjunction in the dry-run assertions with a direct assertion that report.reconciled_no_synthesis is empty. Keep the existing pending-count and planned-create assertions unchanged.

## Finding #681

In @crates/gcore/src/ai/generation/tests/tool_loop.rs around lines 701 - 748, Increase the SlowExecutor sleep duration in tool_timeout_is_recoverable_and_worker_drains_after_loop_continues to provide substantially more than 250 ms between the 1-second tool timeout and worker completion, and raise loop_timeout_seconds accordingly so the loop still completes before its overall deadline. Preserve the existing timeout recovery and pre-drain assertion behavior.

## Finding #682

In @crates/gcore/src/ai_context.rs around lines 80 - 87, Update the non-strict error arm in the ToolLoopLimits::resolve match within the tool-loop limit setup to emit a log::warn! containing the configuration error before returning ToolLoopLimits::default(). Keep strict mode propagating the original error unchanged.

## Finding #725

In @crates/gcode/src/commands/codewiki/text/generation/one_shot.rs around lines 230 - 257, Update generate_with_bounded_retry to honor the retry_after_ms value carried by AiError::RateLimited, using that delay when present instead of the fixed GENERATION_RETRY_BACKOFF value; retain the existing bounded retry behavior and fallback backoff when no server hint is available.

## Finding #726

In @crates/gcode/src/commands/codewiki/text/generation/outcome.rs around lines 144 - 217, Extract the duplicated content-classification match from from_tool_loop and from_daemon_agentic into a private classify_content helper accepting optional content, prompt, and GenerationObservability. Have both constructors pass their content and observability to this helper, preserving the existing None, prompt-echo, refusal, cleaning, and rejection behavior exactly.

## Finding #727

In @crates/gcode/src/commands/codewiki/text/generation/routing.rs around lines 74 - 106, Update resolve_direct_tier_targets so both db::connect_readonly and ai_source_for_conn failures bind their errors and emit diagnostics unless ctx.quiet is enabled, while preserving the existing default DirectTierTargets fallback. Include the underlying error in each log message so connection and configuration failures can be diagnosed.

## Finding #728

In @crates/gcode/src/commands/codewiki/text/generation/routing.rs around lines 64 - 68, Rename the Routing method has_usable_target to all_tiers_usable (or an equivalent name explicitly conveying that every tier is required), and update all callers and references accordingly. Preserve the existing requirement that aggregate, module, and standard each have an api_base.

## Finding #729

In @crates/gcode/src/commands/codewiki/text/generation/tool_loop.rs around lines 159 - 173, The direct-generation target resolution chain is duplicated in resolve_aggregate_direct_target and routing::resolve_direct_tier_targets. Extract the shared connect_readonly, ai_source_for_conn, and resolve_direct_generation_target logic with its silent default handling into a pub(super) helper in routing.rs, then update both call sites to use that helper.

## Finding #730

In @crates/gcode/src/commands/codewiki/text/generation/tool_loop.rs around lines 138 - 157, Preserve the existing unavailable-result behavior while binding and logging errors from CodewikiToolExecutor::new, run_tool_loop, daemon_agentic_chat, and DirectChatTransport::new before degrading. Route diagnostics through the available context logger, honoring ctx.quiet, and include the original error details so transport, executor, and model failures remain distinguishable.

## Finding #731

In @crates/gwiki/src/commands/search.rs around lines 256 - 272, The search evidence construction around page_excluded_from_surfaces and std::fs::read_to_string should skip stale hits whose files no longer exist instead of propagating a WikiError::Io. Detect a missing-file NotFound result for the current page and continue to the next search result, while preserving existing error propagation for other I/O failures and keeping valid evidence processing unchanged.

## Finding #732

In @crates/gwiki/src/page_version.rs around lines 64 - 70, Update yaml_closing_delimiter_start to use the shared frontmatter delimiter parsing rules instead of exact “---\n” checks, including for the opening and closing delimiters. Preserve the existing Option return behavior while recognizing CRLF line endings and delimiters with permitted surrounding whitespace so stamp_generated_page updates the existing frontmatter.
