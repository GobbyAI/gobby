use gobby_core::ai::effective_config::ai_source_for_conn;
use gobby_core::ai_context::AiContext;
#[cfg(test)]
use gobby_core::config::QdrantConfig;
use gobby_core::config::resolve_qdrant_config;
use gobby_core::markdown::frontmatter_body_start;
use gobby_core::token_budget;

use crate::output::{SearchOutput, SearchResultOutput, SearchResultType};
use crate::search as wiki_search;
use crate::support::config::qdrant_config_has_url;
use crate::support::env::database_url_for;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
};
use crate::support::search as search_support;
use crate::support::text::degradation_label;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

/// Narrowing levers suggested when `--token-budget` trims the result set.
const SEARCH_TOKEN_BUDGET_REFINE_HINT: &str = "--limit, a narrower query, or a topic scope";

pub(crate) fn execute(
    query: String,
    selection: ScopeSelection,
    limit: usize,
    include_semantic: bool,
    token_budget: Option<usize>,
    include_candidates: bool,
) -> Result<CommandOutcome, WikiError> {
    render(retrieve(
        query,
        selection,
        limit,
        include_semantic,
        token_budget,
        include_candidates,
    )?)
}

pub(crate) fn retrieve(
    query: String,
    selection: ScopeSelection,
    limit: usize,
    include_semantic: bool,
    token_budget: Option<usize>,
    include_candidates: bool,
) -> Result<SearchOutput, WikiError> {
    let database_url = database_url_for("gwiki search")?
        .ok_or_else(|| WikiError::from(gobby_core::grant::GrantError::DaemonRequired))?;
    let scope = resolve_command_scope(&selection)?;
    run_search_attached(
        &database_url,
        resolved_scope_identity(&scope),
        search_scope_for_resolved(&scope),
        scope.root().to_path_buf(),
        query,
        limit,
        include_semantic,
        token_budget,
        include_candidates,
    )
}

#[allow(
    clippy::too_many_arguments,
    reason = "one-caller wiring fn; a params struct would just restate SearchExecutionInput"
)]
fn run_search_attached(
    database_url: &str,
    output_scope: ScopeIdentity,
    search_scope: wiki_search::SearchScope,
    vault_root: std::path::PathBuf,
    query: String,
    limit: usize,
    include_semantic: bool,
    token_budget: Option<usize>,
    include_candidates: bool,
) -> Result<SearchOutput, WikiError> {
    let mut conn = gobby_core::postgres::connect_readonly(database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki search: {error}"),
        }
    })?;
    let mut source = ai_source_for_conn(&mut conn).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve AI config for gwiki search: {error}"),
    })?;
    let falkor = crate::support::env::falkordb_config()?;
    let semantic_config = if include_semantic {
        let embedding = {
            let ai_context = AiContext::resolve(None, &mut source);
            crate::support::services::resolve_semantic_embedding(&ai_context)
        }
        .ok_or_else(|| required_search_config("embedding endpoint"))?;
        let qdrant = resolve_qdrant_config(&mut source)
            .filter(qdrant_config_has_url)
            .ok_or_else(|| required_search_config("Qdrant"))?;
        Some((embedding, qdrant))
    } else {
        None
    };
    drop(source);
    let mut graph_backend = graph_backend_from_falkor_config(falkor);
    let mut bm25_backend = wiki_search::bm25::PostgresBm25Backend::new(&mut conn);
    let input = SearchExecutionInput {
        output_scope,
        search_scope,
        vault_root,
        query,
        limit,
        include_semantic,
        token_budget,
        include_candidates,
    };
    let Some((embedding, qdrant)) = semantic_config else {
        let mut semantic_backend = search_support::UnavailableSemanticBackend;
        return run_search_with_backends(
            &mut bm25_backend,
            &mut semantic_backend,
            &mut graph_backend,
            input,
        );
    };
    let mut semantic_backend = wiki_search::semantic::GobbySemanticBackend::new(
        Some(embedding),
        Some(qdrant),
        wiki_search::semantic::OpenAiEmbeddingBackend::new(),
        wiki_search::semantic::GobbyQdrantBackend,
    );
    run_search_with_backends(
        &mut bm25_backend,
        &mut semantic_backend,
        &mut graph_backend,
        input,
    )
}

fn graph_backend_from_falkor_config(
    falkor: Option<gobby_core::config::FalkorConfig>,
) -> Box<dyn wiki_search::graph_boost::GraphBoostBackend> {
    let Some(falkor) = falkor else {
        return Box::new(
            wiki_search::graph_boost::UnavailableGraphBoostBackend::unreachable(
                "FalkorDB required infrastructure is not configured; graph search is degraded"
                    .to_string(),
            ),
        );
    };

    match wiki_search::graph_boost::FalkorGraphBoostBackend::new(&falkor) {
        Ok(backend) => Box::new(backend),
        Err(error) => Box::new(
            wiki_search::graph_boost::UnavailableGraphBoostBackend::unreachable(error.to_string()),
        ),
    }
}

fn required_search_config(service: &'static str) -> WikiError {
    WikiError::Config {
        detail: format!(
            "gwiki search requires {service}; run `gwiki setup --standalone` or attach to Gobby's full datastore stack"
        ),
    }
}

struct SearchExecutionInput {
    output_scope: ScopeIdentity,
    search_scope: wiki_search::SearchScope,
    /// Vault root of the resolved scope; result pages excluded from default
    /// retrieval (archived lifecycle) are filtered against the files here.
    vault_root: std::path::PathBuf,
    query: String,
    limit: usize,
    include_semantic: bool,
    token_budget: Option<usize>,
    include_candidates: bool,
}

fn run_search_with_backends<B, S, G>(
    bm25_backend: &mut B,
    semantic_backend: &mut S,
    graph_backend: &mut G,
    input: SearchExecutionInput,
) -> Result<SearchOutput, WikiError>
where
    B: wiki_search::bm25::Bm25SearchBackend,
    S: wiki_search::semantic::SemanticSearchBackend,
    G: wiki_search::graph_boost::GraphBoostBackend,
{
    let response = wiki_search::search(
        bm25_backend,
        semantic_backend,
        graph_backend,
        wiki_search::SearchRequest {
            query: input.query.clone(),
            scope: input.search_scope,
            limit: input.limit,
            include_semantic: input.include_semantic,
        },
    )?;
    let mut results = Vec::with_capacity(response.results.len());
    for result in response.results {
        // Default retrieval excludes archived pages and quarantined
        // candidates (shared default-surface predicate); the vault file is
        // the source of truth so exclusion applies across BM25, semantic,
        // and graph-boost hits alike. `--include-candidates` opts the
        // librarian/upkeep loops back into candidate hits.
        if crate::lifecycle::page_excluded_from_surfaces(
            &input.vault_root,
            std::path::Path::new(&result.path),
            input.include_candidates,
        ) {
            continue;
        }
        let fusion_key = result.fusion_key()?;
        let page_path = input.vault_root.join(&result.path);
        let current_markdown = match std::fs::read_to_string(&page_path) {
            Ok(markdown) => markdown,
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => continue,
            Err(source) => {
                return Err(WikiError::Io {
                    action: "read current wiki search evidence",
                    path: Some(page_path),
                    source,
                });
            }
        };
        let body_start = frontmatter_body_start(&current_markdown).unwrap_or(0);
        let evidence_body = &current_markdown[body_start..];
        let snippet = bounded_snippet(evidence_body, &input.query);
        results.push(SearchResultOutput {
            title: result.title,
            fusion_key,
            result_type: SearchResultType::from_wiki_page(&result.path),
            wiki_page: result.path,
            source_path: result.source_path,
            snippet,
            score: result.score,
            sources: result
                .sources
                .iter()
                .map(|source| source.as_str().to_string())
                .collect(),
            explanations: result
                .explanations
                .iter()
                .map(|explanation| crate::output::SearchSourceExplanationOutput {
                    source: explanation.source.as_str().to_string(),
                    rank: explanation.rank,
                    score: explanation.score,
                })
                .collect(),
        });
    }
    let degradations = response
        .degradations
        .iter()
        .map(degradation_label)
        .collect::<Vec<_>>();
    // Trim the ranked hits to the caller's token budget via the shared
    // gobby-core helper, then keep evidence for the kept prefix only.
    let budgeted = token_budget::trim_results(
        results,
        input.token_budget,
        SEARCH_TOKEN_BUDGET_REFINE_HINT,
        format_search_result_line,
    );
    let results = budgeted.results;
    let mut output = SearchOutput::new(
        input.output_scope,
        input.query,
        input.limit,
        results,
        degradations,
    );
    output.hint = budgeted.hint;
    Ok(output)
}

/// Render one search hit as a single line for `ceil(chars/4)` token estimation.
/// The snippet dominates each row's size, so the budget tracks roughly what the
/// agent-facing JSON costs per result.
fn format_search_result_line(result: &SearchResultOutput) -> String {
    format!(
        "- {} | {} | {}",
        result.wiki_page.display(),
        result.title.as_deref().unwrap_or(""),
        result.snippet
    )
}

/// Max characters of a search display snippet (query-token window).
const SNIPPET_BEFORE_CHARS: usize = 60;
const SNIPPET_AFTER_CHARS: usize = 120;

/// Compact query-token snippet for command output: a whitespace-collapsed
/// window around the first query-token match. Backends hand us chunk content
/// or whole document bodies; output never carries them in full.
fn bounded_snippet(content: &str, query: &str) -> String {
    let window = query_window(content, query, SNIPPET_BEFORE_CHARS, SNIPPET_AFTER_CHARS);
    window.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Character window around the earliest query-token match, falling back to
/// the head of the content when no token matches.
pub(crate) fn query_window(content: &str, query: &str, before: usize, after: usize) -> String {
    let lower_content = content.to_lowercase();
    let match_char_at = query
        .split_whitespace()
        .map(str::to_lowercase)
        .filter(|token| !token.is_empty())
        .filter_map(|token| {
            // The byte index is a char boundary of `lower_content`; its char
            // count approximates the same position in `content` (exact for
            // ASCII, off by at most the rare lowercase expansions).
            lower_content
                .find(&token)
                .map(|byte_index| lower_content[..byte_index].chars().count())
        })
        .min()
        .unwrap_or(0);
    let start = match_char_at.saturating_sub(before);
    let content_len = content.chars().count();
    let end = match_char_at.saturating_add(after).min(content_len);
    content.chars().skip(start).take(end - start).collect()
}

fn render(output: SearchOutput) -> Result<CommandOutcome, WikiError> {
    let scope = output.scope.clone();
    let query = output.query.clone();
    let text = render_text(&query, &scope, &output.results, &output.degradations);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize search output",
        path: None,
        source: error,
    })?;

    Ok(super::scoped_outcome("search", &scope, payload, text))
}

fn render_text(
    query: &str,
    scope: &ScopeIdentity,
    results: &[SearchResultOutput],
    degradations: &[String],
) -> String {
    let mut text = format!("Search results for \"{query}\"\nScope: {scope}\n");
    if !degradations.is_empty() {
        text.push_str(&format!("Degraded: {}\n", degradations.join(", ")));
    }
    if results.is_empty() {
        text.push_str("No results");
        return text;
    }

    for result in results {
        text.push_str("- ");
        text.push_str(&result.wiki_page.display().to_string());
        if let Some(title) = &result.title {
            text.push_str(" | ");
            text.push_str(title);
        }
        text.push_str(" | ");
        text.push_str(&result.snippet);
        text.push('\n');
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn qdrant_config_requires_non_blank_url() {
        assert!(!qdrant_config_has_url(&QdrantConfig {
            url: None,
            api_key: None,
        }));
        assert!(!qdrant_config_has_url(&QdrantConfig {
            url: Some("  ".to_string()),
            api_key: None,
        }));
        assert!(qdrant_config_has_url(&QdrantConfig {
            url: Some("http://qdrant.local".to_string()),
            api_key: None,
        }));
    }

    #[test]
    fn missing_falkor_config_degrades_graph_search() {
        let mut backend = graph_backend_from_falkor_config(None);
        let outcome = backend
            .search_graph_boost(wiki_search::graph_boost::GraphBoostRequest {
                scope: wiki_search::SearchScope::project("project-1"),
                seed_paths: Vec::new(),
                limit: 10,
            })
            .expect("unavailable backend returns degradation");

        assert!(outcome.hits.is_empty());
        let degradation = outcome.degradation.expect("graph degradation");
        assert_eq!(degradation_label(&degradation), "gwiki_graph_unreachable");
        assert!(
            format!("{degradation:?}")
                .contains("FalkorDB required infrastructure is not configured")
        );
    }

    #[test]
    fn bounded_snippet_windows_around_first_query_token() {
        let body = format!(
            "{}ghook enqueues the hook envelope to the inbox before posting.{}",
            "filler ".repeat(200),
            " trailer".repeat(200),
        );
        let snippet = bounded_snippet(&body, "inbox enqueue");

        assert!(snippet.contains("enqueues"));
        assert!(snippet.chars().count() <= SNIPPET_BEFORE_CHARS + SNIPPET_AFTER_CHARS);
    }

    #[test]
    fn bounded_snippet_never_emits_full_document_body() {
        let body = "word ".repeat(5_000);
        let snippet = bounded_snippet(&body, "zzz-no-match");

        assert!(snippet.chars().count() <= SNIPPET_BEFORE_CHARS + SNIPPET_AFTER_CHARS);
        assert!(snippet.starts_with("word"));
    }

    #[test]
    fn document_hit_evidence_strips_frontmatter() {
        let temp = tempfile::tempdir().expect("tempdir");
        let path = "knowledge/concepts/evidence.md";
        let document = temp.path().join(path);
        std::fs::create_dir_all(document.parent().expect("parent")).expect("dirs");
        let body = "Body evidence explains retrieval.";
        let markdown = format!("---\ntitle: Hidden metadata\nlifecycle: verified\n---\n\n{body}\n");
        std::fs::write(&document, &markdown).expect("document");

        let mut hit = store_hit(path);
        hit.snippet = "---\ntitle: Stale index\n---\n\nStale indexed body.\n".to_string();
        let mut bm25_backend = search_support::StoreBm25Backend {
            hits: vec![hit].into(),
        };
        let mut semantic_backend = search_support::UnavailableSemanticBackend;
        let graph = crate::graph::MemoryWikiGraph::default();
        let mut graph_backend = wiki_search::graph_boost::MemoryGraphBoostBackend::new(graph);

        let retrieval = run_search_with_backends(
            &mut bm25_backend,
            &mut semantic_backend,
            &mut graph_backend,
            SearchExecutionInput {
                output_scope: ScopeIdentity::project("project-1"),
                search_scope: wiki_search::SearchScope::project("project-1"),
                vault_root: temp.path().to_path_buf(),
                query: "retrieval".to_string(),
                limit: 10,
                include_semantic: false,
                include_candidates: false,
                token_budget: None,
            },
        )
        .expect("search runs");

        assert_eq!(retrieval.results[0].snippet.trim(), body);
    }

    #[test]
    fn stale_search_hit_for_missing_page_is_skipped() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut bm25_backend = search_support::StoreBm25Backend {
            hits: vec![store_hit("knowledge/concepts/missing.md")].into(),
        };
        let mut semantic_backend = search_support::UnavailableSemanticBackend;
        let graph = crate::graph::MemoryWikiGraph::default();
        let mut graph_backend = wiki_search::graph_boost::MemoryGraphBoostBackend::new(graph);

        let retrieval = run_search_with_backends(
            &mut bm25_backend,
            &mut semantic_backend,
            &mut graph_backend,
            SearchExecutionInput {
                output_scope: ScopeIdentity::project("project-1"),
                search_scope: wiki_search::SearchScope::project("project-1"),
                vault_root: temp.path().to_path_buf(),
                query: "missing".to_string(),
                limit: 10,
                include_semantic: false,
                include_candidates: false,
                token_budget: None,
            },
        )
        .expect("stale hit is ignored");

        assert!(retrieval.results.is_empty());
    }

    #[test]
    fn query_window_handles_multibyte_content() {
        let body = format!("{}évidence enqueue ici{}", "préfixe ".repeat(50), " fin");
        let window = query_window(&body, "enqueue", 10, 20);

        assert!(window.contains("enqueue"));
        assert!(window.chars().count() <= 30);
    }

    fn sample_hit(page: &str, snippet: &str) -> SearchResultOutput {
        SearchResultOutput {
            title: Some(page.to_string()),
            fusion_key: format!("topic:docs:{page}"),
            wiki_page: std::path::PathBuf::from(page),
            source_path: std::path::PathBuf::from(page),
            result_type: SearchResultType::Wiki,
            snippet: snippet.to_string(),
            score: 1.0,
            sources: vec!["bm25".to_string()],
            explanations: Vec::new(),
        }
    }

    #[test]
    fn token_budget_trims_hits_via_shared_helper_and_emits_hint() {
        // Each rendered line is `- a.md | a.md | ` (16 chars) + 80 snippet chars
        // = 96 chars => ceil(96/4) = 24 tokens, so a 30-token budget keeps one hit.
        let hits = vec![
            sample_hit("a.md", &"x".repeat(80)),
            sample_hit("b.md", &"y".repeat(80)),
            sample_hit("c.md", &"z".repeat(80)),
        ];

        let trimmed = token_budget::trim_results(
            hits,
            Some(30),
            SEARCH_TOKEN_BUDGET_REFINE_HINT,
            format_search_result_line,
        );

        assert_eq!(trimmed.results.len(), 1);
        let hint = trimmed
            .hint
            .expect("narrowing hint when results are dropped");
        assert!(hint.contains("1 of 3 results"));
        assert!(hint.contains(SEARCH_TOKEN_BUDGET_REFINE_HINT));
    }

    #[test]
    fn token_budget_none_keeps_all_hits_without_hint() {
        let hits = vec![sample_hit("a.md", "short"), sample_hit("b.md", "short")];

        let trimmed = token_budget::trim_results(
            hits,
            None,
            SEARCH_TOKEN_BUDGET_REFINE_HINT,
            format_search_result_line,
        );

        assert_eq!(trimmed.results.len(), 2);
        assert!(trimmed.hint.is_none());
    }

    fn store_hit(path: &str) -> wiki_search::WikiSearchResult {
        wiki_search::WikiSearchResult {
            id: path.to_string(),
            title: Some(path.to_string()),
            scope: wiki_search::SearchScope::project("project-1"),
            path: path.into(),
            source_path: path.into(),
            hit_kind: wiki_search::SearchHitKind::Document,
            snippet: "lifecycle exclusion evidence".to_string(),
            score: 1.0,
            sources: vec![wiki_search::SearchSource::Bm25],
            explanations: Vec::new(),
            chunk: None,
            provenance: wiki_search::SearchProvenance {
                document_path: path.into(),
                source_path: path.into(),
                source_kind: "concept".to_string(),
                content_hash: None,
            },
        }
    }

    #[test]
    fn default_retrieval_excludes_archived_pages() {
        let temp = tempfile::tempdir().expect("tempdir");
        let live = temp.path().join("knowledge/concepts/live.md");
        std::fs::create_dir_all(live.parent().expect("parent")).expect("dirs");
        std::fs::write(
            &live,
            "---\ntitle: Live\nlifecycle: verified\n---\n\nBody.\n",
        )
        .expect("live page");
        std::fs::write(
            temp.path().join("knowledge/concepts/gone.md"),
            "---\ntitle: Gone\nlifecycle: archived\n---\n\nBody.\n",
        )
        .expect("archived page");

        let mut bm25_backend = search_support::StoreBm25Backend {
            hits: vec![
                store_hit("knowledge/concepts/live.md"),
                store_hit("knowledge/concepts/gone.md"),
            ]
            .into(),
        };
        let mut semantic_backend = search_support::UnavailableSemanticBackend;
        let graph = crate::graph::MemoryWikiGraph::default();
        let mut graph_backend = wiki_search::graph_boost::MemoryGraphBoostBackend::new(graph);

        let retrieval = run_search_with_backends(
            &mut bm25_backend,
            &mut semantic_backend,
            &mut graph_backend,
            SearchExecutionInput {
                output_scope: ScopeIdentity::project("project-1"),
                search_scope: wiki_search::SearchScope::project("project-1"),
                vault_root: temp.path().to_path_buf(),
                query: "lifecycle".to_string(),
                limit: 10,
                include_semantic: false,
                include_candidates: false,
                token_budget: None,
            },
        )
        .expect("search runs");

        let pages: Vec<String> = retrieval
            .results
            .iter()
            .map(|result| result.wiki_page.display().to_string())
            .collect();
        assert_eq!(pages, vec!["knowledge/concepts/live.md".to_string()]);
    }

    fn candidate_vault_retrieval(include_candidates: bool) -> Vec<String> {
        let temp = tempfile::tempdir().expect("tempdir");
        let trusted = temp.path().join("knowledge/concepts/trusted.md");
        std::fs::create_dir_all(trusted.parent().expect("parent")).expect("dirs");
        std::fs::write(
            &trusted,
            "---\ntitle: Trusted\nlifecycle: reviewed\n---\n\nBody.\n",
        )
        .expect("trusted page");
        std::fs::write(
            temp.path().join("knowledge/concepts/quarantined.md"),
            "---\ntitle: Quarantined\nlifecycle: draft\ncandidate: true\n---\n\nBody.\n",
        )
        .expect("candidate page");

        let mut bm25_backend = search_support::StoreBm25Backend {
            hits: vec![
                store_hit("knowledge/concepts/trusted.md"),
                store_hit("knowledge/concepts/quarantined.md"),
            ]
            .into(),
        };
        let mut semantic_backend = search_support::UnavailableSemanticBackend;
        let graph = crate::graph::MemoryWikiGraph::default();
        let mut graph_backend = wiki_search::graph_boost::MemoryGraphBoostBackend::new(graph);

        let retrieval = run_search_with_backends(
            &mut bm25_backend,
            &mut semantic_backend,
            &mut graph_backend,
            SearchExecutionInput {
                output_scope: ScopeIdentity::project("project-1"),
                search_scope: wiki_search::SearchScope::project("project-1"),
                vault_root: temp.path().to_path_buf(),
                query: "lifecycle".to_string(),
                limit: 10,
                include_semantic: false,
                include_candidates,
                token_budget: None,
            },
        )
        .expect("search runs");
        retrieval
            .results
            .iter()
            .map(|result| result.wiki_page.display().to_string())
            .collect()
    }

    #[test]
    fn default_retrieval_excludes_candidate_pages() {
        let pages = candidate_vault_retrieval(false);

        assert_eq!(pages, vec!["knowledge/concepts/trusted.md".to_string()]);
    }

    #[test]
    fn include_candidates_opts_quarantined_pages_back_into_retrieval() {
        let pages = candidate_vault_retrieval(true);

        assert_eq!(
            pages,
            vec![
                "knowledge/concepts/trusted.md".to_string(),
                "knowledge/concepts/quarantined.md".to_string(),
            ]
        );
    }
}
