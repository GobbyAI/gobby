use std::collections::BTreeSet;

use gobby_core::ai::{daemon, effective_route, text};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};

use crate::commands::search;
use crate::output::{AskAiOutput, AskOutput, AskSynthesisOutput, SearchOutput};
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

    let search = search::retrieve(query, selection, DEFAULT_ASK_HIT_LIMIT, true)?;
    let mut output = ask_output_from_search(search);
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
        hits: search.results,
        related_pages,
        sources,
        gaps: Vec::new(),
        stale_candidates: Vec::new(),
        suggested_questions: Vec::new(),
        warnings: search.degradations,
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

fn synthesize(
    output: &mut AskOutput,
    requested_mode: AiRouting,
    require_ai: bool,
) -> Result<(), WikiError> {
    let mut source = ai_config_source()?;
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

fn ai_config_source() -> Result<AiConfigSource, WikiError> {
    let gobby_home = gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki ask config: {error}"),
    })?;
    AiConfigSource::from_gobby_home(&gobby_home).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve AI config for gwiki ask: {error}"),
    })
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
    output.synthesis = Some(AskSynthesisOutput { answer, model });
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
        return prompt;
    }
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
    prompt
}

fn synthesis_system() -> &'static str {
    "Answer the user's question using only the provided wiki hits. Be concise. Say when the wiki does not contain enough evidence."
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
        return format!(
            "Answer for \"{query}\"\nScope: {scope}\n\n{}",
            synthesis.answer
        );
    }
    let mut text = format!("Wiki hits for \"{query}\"\nScope: {scope}\n");
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

    use crate::output::SearchResultOutput;

    use super::*;

    #[test]
    fn ask_output_keeps_full_retrieval_shape() {
        let output = ask_output_from_search(SearchOutput::new(
            ScopeIdentity::topic("docs"),
            "How do hooks work?",
            10,
            vec![SearchResultOutput {
                title: Some("Hooks".to_string()),
                wiki_page: PathBuf::from("wiki/hooks.md"),
                source_path: PathBuf::from("raw/hooks.md"),
                snippet: "Hooks run at turn boundaries.".to_string(),
                score: 0.9,
                sources: vec!["bm25".to_string()],
                explanations: Vec::new(),
            }],
            vec!["semantic_unavailable".to_string()],
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
        assert_eq!(output.warnings, vec!["semantic_unavailable".to_string()]);
        assert!(output.gaps.is_empty());
        assert!(output.stale_candidates.is_empty());
        assert!(output.suggested_questions.is_empty());
        assert!(output.ai.is_none());
        assert!(output.synthesis.is_none());
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
