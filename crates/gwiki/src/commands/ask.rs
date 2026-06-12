use std::collections::{BTreeSet, HashSet};
use std::path::Path;

use gobby_core::ai::{daemon, effective_route, text};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting, FalkorConfig, resolve_falkordb_config};
use gobby_core::gobby_home;

use crate::commands::search;
use crate::graph::WikiGraphFacts;
use crate::graph::context::{GraphContextCodeEdge, GraphContextOptions, build_context_pack};
use crate::output::{
    AskAiOutput, AskCitationCheckOutput, AskCodeCitationOutput, AskCodeEdgeOutput, AskOutput,
    AskRelatedPageOutput, AskSynthesisOutput, SearchOutput, SearchResultOutput,
};
use crate::search::SearchScope;
use crate::support::config::shared_code_graph_limits_from_conn;
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::support::search::PostgresConfigSource;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

const DEFAULT_ASK_HIT_LIMIT: usize = 10;

pub(crate) fn execute(
    query: String,
    selection: ScopeSelection,
    llm: bool,
    ai: AiRouting,
    require_ai: bool,
) -> Result<CommandOutcome, WikiError> {
    if llm && ai == AiRouting::Off {
        return Err(WikiError::InvalidInput {
            field: "ask",
            message: "--llm cannot be combined with --ai off".to_string(),
        });
    }

    let search = search::retrieve(query, selection.clone(), DEFAULT_ASK_HIT_LIMIT, true)?;
    let mut output = ask_output_from_search(search);
    enrich_with_attached_unified_graph_context(&mut output, &selection)?;
    if llm {
        synthesize(&mut output, ai, require_ai)?;
    }
    render(output)
}

fn ask_output_from_search(search: SearchOutput) -> AskOutput {
    let related_pages = search
        .results
        .iter()
        .map(|hit| crate::output::AskRelatedPageOutput {
            title: hit.title.clone(),
            path: hit.wiki_page.clone(),
            score: hit.score,
        })
        .collect::<Vec<_>>();
    let sources = unique_sources(&search);
    let code_citations = code_citations_from_results(&search.results);
    let degraded_sources = ordered_unique_strings(search.degradations.clone());
    let warnings = ordered_unique_strings(search.degradations);
    let status = if search.results.is_empty() {
        "no_results"
    } else {
        "retrieved"
    };
    AskOutput {
        command: "ask",
        scope: search.scope,
        query: search.query,
        status,
        degraded: !degraded_sources.is_empty(),
        degraded_sources,
        hits: search.results,
        related_pages,
        sources,
        code_edges: Vec::new(),
        code_citations,
        truncated: false,
        truncated_components: Vec::new(),
        gaps: Vec::new(),
        stale_candidates: Vec::new(),
        suggested_questions: Vec::new(),
        warnings,
        ai: None,
        synthesis: None,
    }
}

fn unique_sources(search: &SearchOutput) -> Vec<String> {
    let mut seen = BTreeSet::new();
    for hit in &search.results {
        seen.insert(hit.source_path.display().to_string());
        for source in &hit.sources {
            seen.insert(source.clone());
        }
    }
    seen.into_iter().collect()
}

fn code_citations_from_results(results: &[SearchResultOutput]) -> Vec<AskCodeCitationOutput> {
    let mut seen = BTreeSet::new();
    let mut citations = Vec::new();
    for hit in results {
        if !is_code_result(hit) {
            continue;
        }
        let file = hit.source_path.display().to_string();
        let symbol = hit.title.clone();
        if seen.insert((file.clone(), symbol.clone())) {
            citations.push(AskCodeCitationOutput {
                file,
                line: None,
                symbol,
            });
        }
    }
    citations
}

fn enrich_with_attached_unified_graph_context(
    output: &mut AskOutput,
    selection: &ScopeSelection,
) -> Result<(), WikiError> {
    let Some(database_url) = database_url_for("gwiki ask")? else {
        let mut dedup = AskOutputDedup::from_output(output);
        mark_degraded_source(output, &mut dedup, "shared_code_graph_unavailable");
        return Ok(());
    };
    let resolved = resolve_selection_context(selection)?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki ask graph context: {error}"),
        }
    })?;
    let mut facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    let mut degraded_sources = Vec::new();
    let mut truncated_components = Vec::new();
    let limits = shared_code_graph_limits_from_conn(&mut conn)?;
    let falkor = optional_falkor_config(&mut conn)?;
    match (falkor, &resolved.search_scope) {
        (Some(falkor), SearchScope::Project { project_id }) => {
            match crate::falkor_graph::load_code_graph_edges(
                &falkor,
                project_id,
                &facts.documents,
                limits,
            ) {
                Ok(code_graph) => {
                    if code_graph.truncation.is_truncated() {
                        degraded_sources.push(
                            crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string(),
                        );
                        truncated_components.extend(code_graph.truncation.components);
                    }
                    facts.code_edges = code_graph.edges;
                }
                Err(error) => {
                    log::warn!("failed to load shared code graph for gwiki ask: {error}");
                    degraded_sources.push("shared_code_graph_unavailable".to_string());
                }
            }
        }
        (Some(_), SearchScope::Global | SearchScope::Topic { .. }) => {
            degraded_sources.push("shared_code_graph_unavailable".to_string());
        }
        (None, _) => {
            degraded_sources.push("falkordb_unavailable".to_string());
            degraded_sources.push("shared_code_graph_unavailable".to_string());
        }
    }
    enrich_with_unified_graph_context(output, &facts, degraded_sources, truncated_components);
    Ok(())
}

fn optional_falkor_config(conn: &mut postgres::Client) -> Result<Option<FalkorConfig>, WikiError> {
    let gobby_home = gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki ask: {error}"),
    })?;
    let primary = PostgresConfigSource { conn };
    let mut source =
        AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to resolve optional ask graph config: {error}"),
            }
        })?;

    Ok(resolve_falkordb_config(&mut source))
}

fn enrich_with_unified_graph_context(
    output: &mut AskOutput,
    facts: &WikiGraphFacts,
    degraded_sources: Vec<String>,
    truncated_components: Vec<String>,
) {
    let options = if degraded_sources.is_empty() {
        GraphContextOptions::available()
    } else {
        GraphContextOptions::degraded(degraded_sources)
    }
    .with_truncated_components(truncated_components);
    let pack = build_context_pack(facts, options);
    let mut dedup = AskOutputDedup::from_output(output);
    for source in &pack.degradation.degraded_sources {
        mark_degraded_source(output, &mut dedup, source);
    }
    if pack.degradation.truncated {
        output.truncated = true;
        for component in &pack.degradation.truncated_components {
            dedup.push_truncated_component(output, component.clone());
        }
    }

    let mut related_paths = output
        .related_pages
        .iter()
        .map(|page| page.path.clone())
        .collect::<BTreeSet<_>>();
    for neighborhood in &pack.neighborhoods {
        let path = Path::new(&neighborhood.path).to_path_buf();
        if related_paths.insert(path.clone()) {
            output.related_pages.push(AskRelatedPageOutput {
                title: neighborhood.title.clone(),
                path,
                score: 0.0,
            });
        }
        for citation in &neighborhood.citations {
            dedup.push_source(output, citation.clone());
        }
        for edge in neighborhood.calls.iter().chain(neighborhood.imports.iter()) {
            dedup.push_code_edge(output, code_edge_from_context(edge));
        }
        for citation in code_citations_from_context_edges(
            neighborhood.calls.iter().chain(neighborhood.imports.iter()),
        ) {
            dedup.push_code_citation(output, citation);
        }
    }
}

fn mark_degraded_source(output: &mut AskOutput, dedup: &mut AskOutputDedup, source: &str) {
    output.degraded = true;
    dedup.push_degraded_source(output, source.to_string());
}

fn code_citations_from_context_edges<'a>(
    edges: impl Iterator<Item = &'a crate::graph::context::GraphContextCodeEdge>,
) -> Vec<AskCodeCitationOutput> {
    let mut citations = Vec::new();
    for edge in edges {
        if let Some(citation) = code_citation_from_endpoint(&edge.source, edge.line) {
            citations.push(citation);
        }
        if let Some(citation) = code_citation_from_endpoint(&edge.target, None) {
            citations.push(citation);
        }
    }
    citations
}

fn code_edge_from_context(edge: &GraphContextCodeEdge) -> AskCodeEdgeOutput {
    AskCodeEdgeOutput {
        source: edge.source.clone(),
        target: edge.target.clone(),
        kind: edge.kind.clone(),
        direction: edge.direction.clone(),
        line: edge.line,
        provenance: edge.provenance.clone(),
    }
}

fn code_citation_from_endpoint(
    endpoint: &str,
    line: Option<usize>,
) -> Option<AskCodeCitationOutput> {
    let (file, symbol) = match endpoint
        .split_once('#')
        .or_else(|| endpoint.rsplit_once(':'))
    {
        Some((file, symbol)) => (
            file.to_string(),
            (!symbol.is_empty()).then(|| symbol.to_string()),
        ),
        None if endpoint.contains('/') || endpoint.contains('.') => (endpoint.to_string(), None),
        None => return None,
    };
    if file.is_empty() {
        return None;
    }
    Some(AskCodeCitationOutput { file, line, symbol })
}

struct AskOutputDedup {
    sources: HashSet<String>,
    degraded_sources: HashSet<String>,
    truncated_components: HashSet<String>,
    warnings: HashSet<String>,
    code_edges: HashSet<CodeEdgeKey>,
    code_citations: HashSet<CodeCitationKey>,
}

type CodeEdgeKey = (String, String, String, String, Option<usize>, String);
type CodeCitationKey = (String, Option<usize>, Option<String>);

impl AskOutputDedup {
    fn from_output(output: &AskOutput) -> Self {
        Self {
            sources: output.sources.iter().cloned().collect(),
            degraded_sources: output.degraded_sources.iter().cloned().collect(),
            truncated_components: output.truncated_components.iter().cloned().collect(),
            warnings: output.warnings.iter().cloned().collect(),
            code_edges: output.code_edges.iter().map(code_edge_key).collect(),
            code_citations: output
                .code_citations
                .iter()
                .map(code_citation_key)
                .collect(),
        }
    }

    fn push_source(&mut self, output: &mut AskOutput, source: String) {
        if self.sources.insert(source.clone()) {
            output.sources.push(source);
        }
    }

    fn push_degraded_source(&mut self, output: &mut AskOutput, source: String) {
        if self.degraded_sources.insert(source.clone()) {
            output.degraded_sources.push(source.clone());
        }
        self.push_warning(output, source);
    }

    fn push_truncated_component(&mut self, output: &mut AskOutput, component: String) {
        if self.truncated_components.insert(component.clone()) {
            output.truncated_components.push(component);
        }
    }

    fn push_warning(&mut self, output: &mut AskOutput, warning: String) {
        if self.warnings.insert(warning.clone()) {
            output.warnings.push(warning);
        }
    }

    fn push_code_edge(&mut self, output: &mut AskOutput, edge: AskCodeEdgeOutput) {
        if self.code_edges.insert(code_edge_key(&edge)) {
            output.code_edges.push(edge);
        }
    }

    fn push_code_citation(&mut self, output: &mut AskOutput, citation: AskCodeCitationOutput) {
        if self.code_citations.insert(code_citation_key(&citation)) {
            self.push_source(output, citation.file.clone());
            output.code_citations.push(citation);
        }
    }
}

fn ordered_unique_strings(values: Vec<String>) -> Vec<String> {
    let mut seen = HashSet::new();
    values
        .into_iter()
        .filter(|value| seen.insert(value.clone()))
        .collect()
}

fn code_edge_key(edge: &AskCodeEdgeOutput) -> CodeEdgeKey {
    (
        edge.source.clone(),
        edge.target.clone(),
        edge.kind.clone(),
        edge.direction.clone(),
        edge.line,
        edge.provenance.clone(),
    )
}

fn code_citation_key(citation: &AskCodeCitationOutput) -> CodeCitationKey {
    (
        citation.file.clone(),
        citation.line,
        citation.symbol.clone(),
    )
}

fn is_code_result(hit: &SearchResultOutput) -> bool {
    hit.result_type.is_code()
}

fn synthesize(
    output: &mut AskOutput,
    requested_mode: AiRouting,
    require_ai: bool,
) -> Result<(), WikiError> {
    let mut source = crate::support::config::hub_ai_config_source("gwiki ask")?;
    let context = AiContext::resolve_with_options(
        None,
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: Some(requested_mode),
        },
    );
    let route = effective_route(&context, AiCapability::TextGenerate);
    output.ai = Some(AskAiOutput {
        requested: true,
        requested_mode: routing_label(requested_mode),
        route: routing_label(route),
        status: "unavailable",
        model: None,
        error: None,
    });

    match route {
        AiRouting::Direct => generate_direct(output, &context, require_ai),
        AiRouting::Daemon => generate_daemon(output, &context, require_ai),
        AiRouting::Auto | AiRouting::Off => mark_ai_unavailable(output, require_ai, None),
    }
}

fn generate_direct(
    output: &mut AskOutput,
    context: &AiContext,
    require_ai: bool,
) -> Result<(), WikiError> {
    match text::generate_text(context, &synthesis_prompt(output), Some(synthesis_system())) {
        Ok(result) => {
            record_synthesis(output, "direct", result.text, result.model);
            Ok(())
        }
        Err(error) => mark_ai_unavailable(output, require_ai, Some(error.to_string())),
    }
}

fn generate_daemon(
    output: &mut AskOutput,
    context: &AiContext,
    require_ai: bool,
) -> Result<(), WikiError> {
    match daemon::generate_via_daemon(context, &synthesis_prompt(output), Some(synthesis_system()))
    {
        Ok(result) => {
            record_synthesis(output, "daemon", result.text, result.model);
            Ok(())
        }
        Err(error) => mark_ai_unavailable(output, require_ai, Some(error.to_string())),
    }
}

fn record_synthesis(
    output: &mut AskOutput,
    route: &'static str,
    answer: String,
    model: Option<String>,
) {
    let answer = strip_leading_model_narration(&answer);
    output.status = "answered";
    output.ai = Some(AskAiOutput {
        requested: true,
        requested_mode: output
            .ai
            .as_ref()
            .map(|ai| ai.requested_mode)
            .unwrap_or(route),
        route,
        status: "available",
        model: model.clone(),
        error: None,
    });
    let citation_check = citation_check(&answer, output);
    let mut dedup = AskOutputDedup::from_output(output);
    for claim in &citation_check.unsupported_claims {
        dedup.push_warning(
            output,
            format!("synthesis claim lacks citation support in retrieved evidence: {claim}"),
        );
    }
    output.synthesis = Some(AskSynthesisOutput {
        answer,
        model,
        citation_check,
    });
}

/// Maximum leading sentences scanned for a narration preamble. Live escapes
/// run well under this (the #720 sample is 17 sentences); the cap bounds the
/// strip window so a long answer can never lose deep content to a stray
/// late first-person sentence.
const NARRATION_SCAN_LIMIT: usize = 30;

fn strip_leading_model_narration(answer: &str) -> String {
    let original = answer.trim_start();

    // (offset into `original` where the next sentence starts, is_narration)
    let mut spans: Vec<(usize, bool)> = Vec::new();
    let mut cursor = 0usize;
    while spans.len() < NARRATION_SCAN_LIMIT {
        let rest = &original[cursor..];
        let Some(end) = leading_sentence_end(rest) else {
            break;
        };
        let narration = is_model_narration_sentence(rest[..end].trim());
        cursor += end;
        cursor = original.len() - original[cursor..].trim_start().len();
        spans.push((cursor, narration));
    }

    // Only an answer that *opens* with narration is a candidate; a content
    // opener means any later first-person sentence is part of the answer.
    if !spans.first().is_some_and(|&(_, narration)| narration) {
        return original.to_string();
    }

    // Contiguous narration run (the conservative #702 behavior).
    let mut cut = spans
        .iter()
        .take_while(|&&(_, narration)| narration)
        .count();

    // A narration preamble can interleave declarative asides ("The repo root
    // is not the source tree I expected."). Extend the cut to the last
    // narration sentence when narration still dominates the skipped region —
    // a real answer after the run dilutes the density and blocks extension.
    if let Some(last) = spans.iter().rposition(|&(_, narration)| narration)
        && last + 1 > cut
    {
        let narration_count = spans[..=last]
            .iter()
            .filter(|&&(_, narration)| narration)
            .count();
        if narration_count * 3 >= (last + 1) * 2 {
            cut = last + 1;
        }
    }

    let remaining = &original[spans[cut - 1].0..];
    if remaining.is_empty() {
        original.to_string()
    } else {
        remaining.to_string()
    }
}

fn leading_sentence_end(text: &str) -> Option<usize> {
    text.char_indices()
        .find(|(_, ch)| matches!(ch, '.' | '!' | '?'))
        .map(|(index, ch)| index + ch.len_utf8())
}

/// Discourse markers that may precede a first-person narration opener
/// ("First I'll inspect…", "Next, I'm reading…"). Stripped, with an optional
/// comma, before the opener check.
const NARRATION_DISCOURSE_MARKERS: &[&str] = &[
    "first", "next", "now", "then", "so", "ok", "okay", "alright", "finally",
];

/// First-person openers that signal the model is talking about itself rather
/// than answering. Checked after discourse markers are stripped.
const NARRATION_OPENERS: &[&str] = &[
    "i'm ", "i am ", "i'll ", "i will ", "i've ", "i have ", "i found ", "i need ", "i want ",
    "i still ", "i just ", "let me ", "we're ", "we'll ",
];

/// Process vocabulary that distinguishes investigation narration ("I'm
/// pulling the exact lines now.") from a first-person sentence that could be
/// answer content. Matched anywhere in the normalized sentence.
const NARRATION_PROCESS_MARKERS: &[&str] = &[
    "check", "look", "review", "read", "summariz", "answer", "question", "docs", "document",
    "provided", "evidence", "retriev", "wiki", "inspect", "confirm", "verif", "pull", "grab",
    "search", "scan", "fall", "dig", "examin", "found", "code", "pass", "context", "layout",
];

fn is_model_narration_sentence(sentence: &str) -> bool {
    let normalized = sentence
        .trim()
        .replace(['\u{2018}', '\u{2019}'], "'")
        .to_ascii_lowercase();
    let opener = strip_narration_discourse_markers(&normalized);
    let starts_like_narration = NARRATION_OPENERS
        .iter()
        .any(|prefix| opener.starts_with(prefix));
    let describes_process = NARRATION_PROCESS_MARKERS
        .iter()
        .any(|marker| normalized.contains(marker));

    starts_like_narration && describes_process
}

fn strip_narration_discourse_markers(normalized: &str) -> &str {
    let mut rest = normalized;
    loop {
        let mut advanced = false;
        for marker in NARRATION_DISCOURSE_MARKERS {
            if let Some(tail) = rest.strip_prefix(marker) {
                let tail = tail.strip_prefix(',').unwrap_or(tail);
                if let Some(tail) = tail.strip_prefix(' ') {
                    rest = tail.trim_start();
                    advanced = true;
                    break;
                }
            }
        }
        if !advanced {
            return rest;
        }
    }
}

/// Grounding statuses for [`citation_check`]: every extracted claim overlaps
/// the retrieved evidence, or at least one claim does not.
const CITATION_CHECK_SUPPORTED: &str = "supported";
const CITATION_CHECK_UNSUPPORTED: &str = "unsupported_claims";

/// Minimum fraction of a claim's significant tokens that must appear in the
/// retrieved evidence for the claim to count as grounded.
const CLAIM_SUPPORT_THRESHOLD: f64 = 0.5;

/// Claims with fewer significant tokens than this are skipped — short hedges
/// like "The evidence is insufficient." carry no checkable factual content.
const MIN_CLAIM_TOKENS: usize = 3;

/// Check the synthesized answer's claims against the retrieved evidence.
///
/// The synthesis prompt only *asks* the model to stay grounded; this is the
/// post-generation verification. Each sentence-level claim is tokenized and
/// must overlap the evidence corpus (hit titles/snippets/paths, related pages,
/// code citations) above [`CLAIM_SUPPORT_THRESHOLD`], mirroring the spirit of
/// `audit`'s provenance check for persisted prose.
fn citation_check(answer: &str, output: &AskOutput) -> AskCitationCheckOutput {
    let evidence = evidence_tokens(output);
    let claims = answer_claims(answer);
    let checked_claims = claims.len();
    let unsupported_claims: Vec<String> = claims
        .into_iter()
        .filter(|claim| !claim_is_supported(claim, &evidence))
        .collect();
    AskCitationCheckOutput {
        status: if unsupported_claims.is_empty() {
            CITATION_CHECK_SUPPORTED
        } else {
            CITATION_CHECK_UNSUPPORTED
        },
        checked_claims,
        unsupported_claims,
    }
}

/// Split the answer into sentence-level claims with enough significant tokens
/// to be checkable. Markdown headings and list markers are stripped first.
fn answer_claims(answer: &str) -> Vec<String> {
    answer
        .lines()
        .map(|line| line.trim().trim_start_matches(['#', '-', '*', '>']).trim())
        .filter(|line| !line.is_empty())
        .flat_map(|line| line.split_inclusive(['.', '!', '?']))
        .map(|sentence| {
            sentence
                .trim()
                .trim_end_matches(['.', '!', '?'])
                .to_string()
        })
        .filter(|sentence| significant_tokens(sentence).len() >= MIN_CLAIM_TOKENS)
        .collect()
}

fn claim_is_supported(claim: &str, evidence: &HashSet<String>) -> bool {
    let tokens = significant_tokens(claim);
    if tokens.is_empty() {
        return true;
    }
    let supported = tokens
        .iter()
        .filter(|token| evidence.contains(token.as_str()))
        .count();
    (supported as f64 / tokens.len() as f64) >= CLAIM_SUPPORT_THRESHOLD
}

fn evidence_tokens(output: &AskOutput) -> HashSet<String> {
    let mut evidence = HashSet::new();
    for hit in &output.hits {
        if let Some(title) = &hit.title {
            collect_tokens(title, &mut evidence);
        }
        collect_tokens(&hit.snippet, &mut evidence);
        collect_tokens(&hit.wiki_page.display().to_string(), &mut evidence);
        collect_tokens(&hit.source_path.display().to_string(), &mut evidence);
    }
    for page in &output.related_pages {
        if let Some(title) = &page.title {
            collect_tokens(title, &mut evidence);
        }
        collect_tokens(&page.path.display().to_string(), &mut evidence);
    }
    for citation in &output.code_citations {
        collect_tokens(&citation.file, &mut evidence);
        if let Some(symbol) = &citation.symbol {
            collect_tokens(symbol, &mut evidence);
        }
    }
    evidence
}

fn significant_tokens(text: &str) -> Vec<String> {
    let mut tokens: Vec<String> = Vec::new();
    collect_tokens_into(text, |token| tokens.push(token));
    tokens
}

fn collect_tokens(text: &str, evidence: &mut HashSet<String>) {
    collect_tokens_into(text, |token| {
        evidence.insert(token);
    });
}

/// Tokenize into lowercase alphanumeric runs, keeping only words long enough
/// to carry meaning and dropping common function words.
fn collect_tokens_into(text: &str, mut push: impl FnMut(String)) {
    const STOPWORDS: &[&str] = &[
        "about", "after", "also", "based", "because", "been", "before", "being", "between", "both",
        "does", "each", "either", "from", "have", "into", "its", "more", "most", "only", "other",
        "over", "same", "should", "since", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "through", "under", "uses", "using", "very", "when",
        "where", "which", "while", "will", "with", "within", "would",
    ];
    for token in text
        .split(|c: char| !c.is_alphanumeric())
        .filter(|token| token.len() >= 4)
    {
        let token = token.to_lowercase();
        if !STOPWORDS.contains(&token.as_str()) {
            push(token);
        }
    }
}

fn mark_ai_unavailable(
    output: &mut AskOutput,
    require_ai: bool,
    error: Option<String>,
) -> Result<(), WikiError> {
    if require_ai {
        return Err(WikiError::Config {
            detail: error.unwrap_or_else(|| "AI synthesis is unavailable".to_string()),
        });
    }
    output.status = "partial";
    output.degraded = true;
    if !output
        .degraded_sources
        .iter()
        .any(|source| source == "model_provider_unavailable")
    {
        output
            .degraded_sources
            .push("model_provider_unavailable".to_string());
    }
    if !output
        .warnings
        .iter()
        .any(|warning| warning == "ai_unavailable")
    {
        output.warnings.push("ai_unavailable".to_string());
    }
    if let Some(ai) = &mut output.ai {
        ai.error = error;
    }
    Ok(())
}

fn synthesis_prompt(output: &AskOutput) -> String {
    let mut prompt = format!("Question: {}\n\nWiki hits:\n", output.query);
    if output.hits.is_empty() {
        prompt.push_str("No wiki hits were found.\n");
    } else {
        for (index, hit) in output.hits.iter().enumerate() {
            let title = hit.title.as_deref().unwrap_or("Untitled");
            prompt.push_str(&format!(
                "{}. {} ({})\nSource: {}\nSnippet: {}\n\n",
                index + 1,
                title,
                hit.wiki_page.display(),
                hit.source_path.display(),
                hit.snippet
            ));
        }
    }
    if !output.related_pages.is_empty() || !output.code_citations.is_empty() {
        prompt.push_str("Unified graph context:\n");
        for page in &output.related_pages {
            let title = page.title.as_deref().unwrap_or("Untitled");
            prompt.push_str(&format!("- {} ({})\n", title, page.path.display()));
        }
        prompt.push('\n');
    }
    if !output.code_citations.is_empty() {
        prompt.push_str("Code citations:\n");
        for citation in &output.code_citations {
            let symbol = citation.symbol.as_deref().unwrap_or("unknown symbol");
            match citation.line {
                Some(line) => {
                    prompt.push_str(&format!("- {}:{} ({})\n", citation.file, line, symbol))
                }
                None => prompt.push_str(&format!("- {} ({})\n", citation.file, symbol)),
            }
        }
        prompt.push('\n');
    }
    prompt
}

fn synthesis_system() -> &'static str {
    "Answer the user's question using only the provided wiki hits, unified graph context, and code citations. Write only the final answer; do not describe your process, plan, search, or retrieval steps. Be concise. Say when the evidence is insufficient."
}

fn render(output: AskOutput) -> Result<CommandOutcome, WikiError> {
    let scope = output.scope.clone();
    let text = render_text(&output.query, &scope, &output);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize ask output",
        path: None,
        source: error,
    })?;

    Ok(super::scoped_outcome("ask", &scope, payload, text))
}

fn render_text(query: &str, scope: &ScopeIdentity, output: &AskOutput) -> String {
    if let Some(synthesis) = &output.synthesis {
        let mut text = format!(
            "Answer for \"{query}\"\nScope: {scope}\n\n{}",
            synthesis.answer
        );
        if synthesis.citation_check.status != CITATION_CHECK_SUPPORTED {
            text.push_str(&format!(
                "\n\n[unverified] {} claim(s) lack citation support in the retrieved evidence.",
                synthesis.citation_check.unsupported_claims.len()
            ));
        }
        return text;
    }
    let mut text = format!("Wiki hits for \"{query}\"\nScope: {scope}\n");
    if output.degraded {
        text.push_str(&format!(
            "Degraded sources: {}\n",
            output.degraded_sources.join(", ")
        ));
    }
    if output.hits.is_empty() {
        text.push_str("No results");
    } else {
        for hit in &output.hits {
            text.push_str("- ");
            if let Some(title) = &hit.title {
                text.push_str(title);
                text.push_str(" — ");
            }
            text.push_str(&hit.wiki_page.display().to_string());
            text.push('\n');
        }
    }
    if !output.code_citations.is_empty() {
        text.push_str("Code citations\n");
        for citation in &output.code_citations {
            text.push_str("- ");
            text.push_str(&citation.file);
            if let Some(line) = citation.line {
                text.push_str(&format!(":{line}"));
            }
            if let Some(symbol) = &citation.symbol {
                text.push(' ');
                text.push_str(symbol);
            }
            text.push('\n');
        }
    }
    text
}

fn routing_label(route: AiRouting) -> &'static str {
    match route {
        AiRouting::Auto => "auto",
        AiRouting::Daemon => "daemon",
        AiRouting::Direct => "direct",
        AiRouting::Off => "off",
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::graph::{
        WikiGraphCodeEdge, WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphLinkTarget,
        WikiGraphSource,
    };
    use crate::output::{SearchResultOutput, SearchResultType};
    use crate::search::SearchScope;

    use super::*;

    #[test]
    fn ask_output_keeps_full_retrieval_shape() {
        let output = ask_output_from_search(SearchOutput::new(
            ScopeIdentity::topic("docs"),
            "How do hooks work?",
            10,
            vec![SearchResultOutput {
                title: Some("Hooks".to_string()),
                fusion_key: "topic:docs:wiki/hooks.md".to_string(),
                wiki_page: PathBuf::from("wiki/hooks.md"),
                source_path: PathBuf::from("raw/hooks.md"),
                result_type: SearchResultType::Wiki,
                snippet: "Hooks run at turn boundaries.".to_string(),
                score: 0.9,
                sources: vec!["bm25".to_string()],
                explanations: Vec::new(),
            }],
            vec![
                "semantic_unavailable".to_string(),
                "semantic_unavailable".to_string(),
            ],
        ));

        assert_eq!(output.command, "ask");
        assert_eq!(output.status, "retrieved");
        assert_eq!(output.query, "How do hooks work?");
        assert_eq!(output.hits.len(), 1);
        assert_eq!(output.related_pages[0].path, PathBuf::from("wiki/hooks.md"));
        assert_eq!(
            output.sources,
            vec!["bm25".to_string(), "raw/hooks.md".to_string()]
        );
        assert!(output.code_edges.is_empty());
        assert_eq!(output.warnings, vec!["semantic_unavailable".to_string()]);
        assert_eq!(
            output.degraded_sources,
            vec!["semantic_unavailable".to_string()]
        );
        assert!(output.gaps.is_empty());
        assert!(output.stale_candidates.is_empty());
        assert!(output.suggested_questions.is_empty());
        assert!(output.ai.is_none());
        assert!(output.synthesis.is_none());
    }

    #[test]
    fn ask_unified_graph_output_carries_code_citations_and_degradation() {
        let output = ask_output_from_search(SearchOutput::new(
            ScopeIdentity::project("project-1"),
            "Where is request handling wired?",
            10,
            vec![
                SearchResultOutput {
                    title: Some("Request handler".to_string()),
                    fusion_key: "project:project-1:code/files/src/handler.rs.md".to_string(),
                    wiki_page: PathBuf::from("code/files/src/handler.rs.md"),
                    source_path: PathBuf::from("src/handler.rs"),
                    result_type: SearchResultType::Code,
                    snippet: "fn handle() calls route().".to_string(),
                    score: 0.95,
                    sources: vec!["bm25".to_string(), "graph".to_string()],
                    explanations: Vec::new(),
                },
                SearchResultOutput {
                    title: Some("Architecture".to_string()),
                    fusion_key: "project:project-1:wiki/architecture.md".to_string(),
                    wiki_page: PathBuf::from("wiki/architecture.md"),
                    source_path: PathBuf::from("raw/architecture.md"),
                    result_type: SearchResultType::Wiki,
                    snippet: "The handler delegates routing.".to_string(),
                    score: 0.85,
                    sources: vec!["graph".to_string()],
                    explanations: Vec::new(),
                },
            ],
            vec!["shared_code_graph_unavailable".to_string()],
        ));

        assert!(output.degraded);
        assert_eq!(
            output.degraded_sources,
            vec!["shared_code_graph_unavailable".to_string()]
        );
        assert!(!output.truncated);
        assert!(output.truncated_components.is_empty());
        assert_eq!(output.code_citations.len(), 1);
        assert_eq!(output.code_citations[0].file, "src/handler.rs");
        assert_eq!(output.code_citations[0].line, None);
        assert_eq!(
            output.code_citations[0].symbol,
            Some("Request handler".to_string())
        );

        let prompt = synthesis_prompt(&output);
        assert!(prompt.contains("Code citations:"));
        assert!(prompt.contains("src/handler.rs"));
        assert!(prompt.contains("Request handler"));
    }

    #[test]
    fn ask_unified_graph_enrichment_uses_context_pack_code_edges_and_degrades() {
        let mut output = ask_output_from_search(SearchOutput::new(
            ScopeIdentity::project("project-1"),
            "Where is request handling wired?",
            10,
            vec![SearchResultOutput {
                title: Some("Architecture".to_string()),
                fusion_key: "project:project-1:wiki/architecture.md".to_string(),
                wiki_page: PathBuf::from("wiki/architecture.md"),
                source_path: PathBuf::from("raw/architecture.md"),
                result_type: SearchResultType::Wiki,
                snippet: "The handler delegates routing.".to_string(),
                score: 0.85,
                sources: vec!["bm25".to_string()],
                explanations: Vec::new(),
            }],
            Vec::new(),
        ));
        let scope = SearchScope::project("project-1");
        let facts = WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: PathBuf::from("wiki/architecture.md"),
                    title: Some("Architecture".to_string()),
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: PathBuf::from("code/files/src/handler.rs.md"),
                    title: Some("Request handler".to_string()),
                },
            ],
            links: vec![WikiGraphLink {
                scope: scope.clone(),
                source_path: PathBuf::from("wiki/architecture.md"),
                raw_target: "handler".to_string(),
                target: WikiGraphLinkTarget::Resolved(PathBuf::from(
                    "code/files/src/handler.rs.md",
                )),
            }],
            sources: vec![WikiGraphSource {
                scope: scope.clone(),
                source_path: PathBuf::from("src/handler.rs"),
                document_path: PathBuf::from("code/files/src/handler.rs.md"),
            }],
            code_edges: vec![WikiGraphCodeEdge {
                scope: scope.clone(),
                document_path: PathBuf::from("code/files/src/handler.rs.md"),
                source: "src/handler.rs:handle".to_string(),
                target: "src/router.rs:route".to_string(),
                kind: "calls".to_string(),
                direction: "outgoing".to_string(),
                line: Some(42),
                provenance: "gcode_falkor".to_string(),
            }],
        };

        enrich_with_unified_graph_context(
            &mut output,
            &facts,
            vec![
                crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string(),
                crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string(),
            ],
            vec![
                "code_call_edges>7".to_string(),
                "code_call_edges>7".to_string(),
            ],
        );

        assert!(output.degraded);
        assert_eq!(
            output.degraded_sources,
            vec![crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string()]
        );
        assert!(output.truncated);
        assert_eq!(
            output.truncated_components,
            vec!["code_call_edges>7".to_string()]
        );
        assert!(output.related_pages.iter().any(|page| {
            page.path == Path::new("code/files/src/handler.rs.md")
                && page.title.as_deref() == Some("Request handler")
        }));
        assert!(output.code_edges.iter().any(|edge| {
            edge.source == "src/handler.rs:handle"
                && edge.target == "src/router.rs:route"
                && edge.kind == "calls"
                && edge.direction == "outgoing"
                && edge.line == Some(42)
                && edge.provenance == "gcode_falkor"
        }));
        assert!(output.code_citations.iter().any(|citation| {
            citation.file == "src/handler.rs"
                && citation.line == Some(42)
                && citation.symbol.as_deref() == Some("handle")
        }));

        let prompt = synthesis_prompt(&output);
        assert!(prompt.contains("Unified graph context:"));
        assert!(prompt.contains("code/files/src/handler.rs.md"));
        assert!(prompt.contains("src/handler.rs:42 (handle)"));
    }

    #[test]
    fn ask_unified_graph_model_unavailable_marks_degraded() {
        let mut output = ask_output_from_search(SearchOutput::new(
            ScopeIdentity::project("project-1"),
            "Can this synthesize?",
            10,
            Vec::new(),
            Vec::new(),
        ));

        mark_ai_unavailable(&mut output, false, Some("no model".to_string()))
            .expect("model unavailable should degrade without require_ai");

        assert!(output.degraded);
        assert_eq!(
            output.degraded_sources,
            vec!["model_provider_unavailable".to_string()]
        );
        assert_eq!(output.warnings, vec!["ai_unavailable".to_string()]);
    }

    fn output_with_hooks_hit() -> AskOutput {
        ask_output_from_search(SearchOutput::new(
            ScopeIdentity::topic("docs"),
            "How do hooks work?",
            10,
            vec![SearchResultOutput {
                title: Some("Hooks".to_string()),
                fusion_key: "topic:docs:wiki/hooks.md".to_string(),
                wiki_page: PathBuf::from("wiki/hooks.md"),
                source_path: PathBuf::from("raw/hooks.md"),
                result_type: SearchResultType::Wiki,
                snippet: "Hooks run at turn boundaries and dispatch envelopes to the daemon."
                    .to_string(),
                score: 0.9,
                sources: vec!["bm25".to_string()],
                explanations: Vec::new(),
            }],
            Vec::new(),
        ))
    }

    #[test]
    fn synthesis_with_ungrounded_claim_is_flagged_in_json() {
        let mut output = output_with_hooks_hit();

        record_synthesis(
            &mut output,
            "direct",
            "Hooks run at turn boundaries and dispatch envelopes to the daemon. \
             Kubernetes pods restart the scheduler cluster nightly."
                .to_string(),
            Some("test-model".to_string()),
        );

        let synthesis = output.synthesis.as_ref().expect("synthesis recorded");
        assert_eq!(synthesis.citation_check.status, "unsupported_claims");
        assert_eq!(synthesis.citation_check.checked_claims, 2);
        assert_eq!(
            synthesis.citation_check.unsupported_claims,
            vec!["Kubernetes pods restart the scheduler cluster nightly".to_string()]
        );
        assert_eq!(
            output.warnings,
            vec![
                "synthesis claim lacks citation support in retrieved evidence: \
                 Kubernetes pods restart the scheduler cluster nightly"
                    .to_string()
            ]
        );

        let json = serde_json::to_value(&output).expect("serialize ask output");
        assert_eq!(
            json["synthesis"]["citation_check"]["status"],
            "unsupported_claims"
        );
        assert_eq!(
            json["synthesis"]["citation_check"]["unsupported_claims"][0],
            "Kubernetes pods restart the scheduler cluster nightly"
        );
        assert!(
            json["warnings"][0]
                .as_str()
                .expect("warning is a string")
                .contains("lacks citation support")
        );
    }

    #[test]
    fn synthesis_strips_leading_model_narration_before_recording() {
        let mut output = output_with_hooks_hit();

        record_synthesis(
            &mut output,
            "daemon",
            "I'm checking the codewiki docs just enough to answer which page types it emits, \
             then I'll summarize the set precisely.I've got the documented page categories already. \
             Hooks run at turn boundaries and dispatch envelopes to the daemon."
                .to_string(),
            Some("test-model".to_string()),
        );

        let synthesis = output.synthesis.as_ref().expect("synthesis recorded");
        assert_eq!(
            synthesis.answer,
            "Hooks run at turn boundaries and dispatch envelopes to the daemon."
        );
        assert_eq!(synthesis.citation_check.status, "supported");
        assert_eq!(synthesis.citation_check.checked_claims, 1);
        assert!(synthesis.citation_check.unsupported_claims.is_empty());
        assert!(output.warnings.is_empty());
    }

    /// Verbatim live escape from the #720 sample (`/tmp/ask-e2e.json`): a
    /// 17-sentence planning preamble with interleaved declarative asides
    /// that defeat run-based stripping.
    #[test]
    fn interleaved_narration_preamble_is_stripped_to_answer() {
        let answer = concat!(
            "First I\u{2019}ll inspect the transport and failure-handling code around ",
            "inbox/quarantine.`gcode` can\u{2019}t reach the local hub in this environment, ",
            "so I\u{2019}m falling back to direct source reads. I\u{2019}m looking for the ",
            "exact branch that handles enqueue failures and whether it exits, quarantines, ",
            "or silently degrades.The repo root in this workspace is not the raw crate ",
            "source tree I expected. I\u{2019}m checking the actual layout so I can read the ",
            "right files instead of guessing paths from the wiki index.I found the ",
            "daemon-side inbox replay code. Next I\u{2019}m reading the quarantine and replay ",
            "branches plus the regression tests so I can tell you whether a failed enqueue ",
            "drops, quarantines, or retries the envelope.The daemon replay side is clear: ",
            "failures there are retried later, while malformed envelopes are quarantined. ",
            "I still need to confirm the client-side ghook enqueue path, because ",
            "that\u{2019}s the more likely reading of your question.I\u{2019}ve confirmed the ",
            "daemon replay semantics. Now I\u{2019}m checking the enqueue-first contract in ",
            "the docs, because that\u{2019}s where the client-side failure mode is ",
            "specified.I have enough context to answer, but I want line-accurate references ",
            "for the two distinct failure modes: client enqueue/post failure versus daemon ",
            "replay failure. I\u{2019}m pulling the exact lines now.I\u{2019}ve got the code ",
            "path. I\u{2019}m doing one last pass for a memory hook, because this repo asks ",
            "agents to persist durable codebase facts when they learn one.I found the key ",
            "implementation gap: the current daemon code keeps failed replay files in the ",
            "inbox, while the planning docs describe a future backoff/quarantine policy. ",
            "I\u{2019}m grabbing the regression test line numbers to back that up precisely.",
            "The current code path does not drop a replayable inbox file on failure.",
        );

        assert_eq!(
            strip_leading_model_narration(answer),
            "The current code path does not drop a replayable inbox file on failure."
        );
    }

    #[test]
    fn content_opener_disables_narration_stripping() {
        let answer = "Hooks run at turn boundaries. The envelope is enqueued before the POST. \
                      I'm checking the wiki docs for details.";
        assert_eq!(strip_leading_model_narration(answer), answer);
    }

    #[test]
    fn low_narration_density_strips_only_the_leading_run() {
        let answer = "I'm checking the wiki docs before answering. Hooks run at turn \
                      boundaries. Envelopes are enqueued before the daemon POST. Failures \
                      leave the file in place for the next drain pass. Malformed envelopes \
                      are quarantined with a sidecar. I'm reading the contract docs for the \
                      exact field names.";
        let stripped = strip_leading_model_narration(answer);
        assert!(stripped.starts_with("Hooks run at turn boundaries."));
        assert!(stripped.ends_with("I'm reading the contract docs for the exact field names."));
    }

    #[test]
    fn all_narration_answer_is_kept_verbatim() {
        let answer = "I'm checking the wiki docs now.";
        assert_eq!(strip_leading_model_narration(answer), answer);
    }

    #[test]
    fn discourse_markers_before_first_person_openers_are_narration() {
        for sentence in [
            "First I\u{2019}ll inspect the transport and failure-handling code.",
            "Next I\u{2019}m reading the quarantine and replay branches.",
            "Now, let me check the enqueue contract.",
            "I found the daemon-side inbox replay code.",
            "I still need to confirm the client-side ghook enqueue path.",
        ] {
            assert!(
                is_model_narration_sentence(sentence),
                "expected narration: {sentence}"
            );
        }
        for sentence in [
            "The daemon retries failed replays later.",
            "First, configure the daemon endpoint.",
            "I expected the inbox to drop the file.",
            "Failures leave the file in place for the next drain pass.",
        ] {
            assert!(
                !is_model_narration_sentence(sentence),
                "expected content: {sentence}"
            );
        }
    }

    #[test]
    fn synthesis_system_requests_answer_only_output() {
        let system = synthesis_system();

        assert!(system.contains("Write only the final answer"));
        assert!(system.contains("do not describe your process"));
        assert!(system.contains("retrieval steps"));
    }

    #[test]
    fn synthesis_grounded_in_hits_passes_citation_check() {
        let mut output = output_with_hooks_hit();

        record_synthesis(
            &mut output,
            "daemon",
            "Hooks dispatch envelopes to the daemon at turn boundaries. \
             The evidence is thin."
                .to_string(),
            None,
        );

        let synthesis = output.synthesis.as_ref().expect("synthesis recorded");
        assert_eq!(synthesis.citation_check.status, "supported");
        assert_eq!(synthesis.citation_check.checked_claims, 1);
        assert!(synthesis.citation_check.unsupported_claims.is_empty());
        assert!(output.warnings.is_empty());
    }

    #[test]
    fn unverified_synthesis_is_flagged_in_text_render() {
        let mut output = output_with_hooks_hit();
        record_synthesis(
            &mut output,
            "direct",
            "Kubernetes pods restart the scheduler cluster nightly.".to_string(),
            None,
        );

        let text = render_text(
            &output.query.clone(),
            &ScopeIdentity::topic("docs"),
            &output,
        );
        assert!(text.contains("[unverified] 1 claim(s) lack citation support"));

        let mut grounded = output_with_hooks_hit();
        record_synthesis(
            &mut grounded,
            "direct",
            "Hooks run at turn boundaries.".to_string(),
            None,
        );
        let text = render_text(
            &grounded.query.clone(),
            &ScopeIdentity::topic("docs"),
            &grounded,
        );
        assert!(!text.contains("[unverified]"));
    }

    #[test]
    fn llm_ai_off_is_invalid_input() {
        let error = execute(
            "Question?".to_string(),
            ScopeSelection::detect(),
            true,
            AiRouting::Off,
            false,
        )
        .expect_err("ask --llm --ai off should fail before retrieval");

        assert!(matches!(
            error,
            WikiError::InvalidInput { field: "ask", .. }
        ));
    }
}
