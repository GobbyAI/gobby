use std::path::Path;

use gobby_core::ai::{daemon, effective_route, text};
use gobby_core::ai_context::{AiConfigSource, AiContext, AiContextOptions};
use gobby_core::config::{AiCapability, AiRouting};

use super::AcceptedNoteDraft;
use super::notes::write_accepted_note;
use super::outcome::{
    dedup_code_citations, dedup_strings, estimate_tokens, observation_from_outcome,
};
use crate::commands::{ask, index, read, search};
use crate::research_loop::{
    ModelDecision, ModelRequest, NoteWriteOutcome, ResearchModel, ResearchModelError,
    ResearchNoteWriter, ResearchObservation, SourceIngestor, WikiAsk, WikiRead, WikiSearch,
    model_system_prompt, parse_model_action, render_model_prompt,
};
use crate::session::ResearchCodeCitation;
use crate::{IngestFileOptions, ReadTarget, ScopeSelection, WikiError};

pub(crate) struct GcoreResearchModel {
    pub(crate) requested_route: AiRouting,
    pub(crate) require_ai: bool,
}

impl GcoreResearchModel {
    fn ai_unavailable<T>(&self, message: String) -> Result<T, ResearchModelError> {
        if self.require_ai {
            return Err(WikiError::Config { detail: message }.into());
        }
        Err(ResearchModelError::AiUnavailable(message))
    }
}

impl ResearchModel for GcoreResearchModel {
    fn next_action(
        &mut self,
        request: ModelRequest<'_>,
    ) -> Result<ModelDecision, ResearchModelError> {
        let mut source = research_ai_config_source()?;
        let context = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                no_ai: false,
                forced_routing: Some(self.requested_route),
            },
        );
        let route = effective_route(&context, AiCapability::TextGenerate);
        let prompt = render_model_prompt(&request);
        let max_tokens = request.max_tokens.saturating_sub(request.tokens_used);
        if max_tokens == 0 {
            return Err(ResearchModelError::BudgetExceeded);
        }
        let result = match route {
            AiRouting::Direct => text::generate_text_with_max_tokens(
                &context,
                &prompt,
                Some(model_system_prompt()),
                Some(max_tokens),
            ),
            AiRouting::Daemon => daemon::generate_via_daemon_with_max_tokens(
                &context,
                &prompt,
                Some(model_system_prompt()),
                Some(max_tokens),
            ),
            AiRouting::Off => {
                return self
                    .ai_unavailable(format!("text generation route '{route:?}' is unavailable"));
            }
            AiRouting::Auto => {
                return self
                    .ai_unavailable(format!("text generation route '{route:?}' is unavailable"));
            }
        };

        let result = match result {
            Ok(result) => result,
            Err(error) => return self.ai_unavailable(error.to_string()),
        };
        let action =
            parse_model_action(&result.text).map_err(ResearchModelError::InvalidResponse)?;
        let tokens_used = result
            .usage
            .as_ref()
            .and_then(|usage| usage.token_count())
            .unwrap_or_else(|| {
                estimate_tokens(&prompt).saturating_add(estimate_tokens(&result.text))
            });
        Ok(ModelDecision {
            action,
            tokens_used,
        })
    }
}

pub(crate) fn research_ai_config_source() -> Result<AiConfigSource, WikiError> {
    let gobby_home = gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki research config: {error}"),
    })?;
    AiConfigSource::from_gobby_home(&gobby_home).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve AI config for gwiki research: {error}"),
    })
}

pub(crate) struct CommandAsk {
    pub(crate) selection: ScopeSelection,
}

impl WikiAsk for CommandAsk {
    fn ask(&mut self, query: &str) -> Result<ResearchObservation, WikiError> {
        let outcome = ask::execute(
            query.to_string(),
            self.selection.clone(),
            false,
            AiRouting::Off,
            false,
        )?;
        Ok(observation_from_outcome("ask", &outcome))
    }
}

pub(crate) struct CommandSearch {
    pub(crate) selection: ScopeSelection,
}

impl WikiSearch for CommandSearch {
    fn search(&mut self, query: &str, limit: usize) -> Result<ResearchObservation, WikiError> {
        let output = search::retrieve(query.to_string(), self.selection.clone(), limit, true)?;
        let mut sources = Vec::new();
        for result in &output.results {
            sources.push(result.source_path.display().to_string());
            sources.extend(result.sources.iter().cloned());
        }
        Ok(ResearchObservation::new(
            "search",
            format!("{} search hit(s) for {query}", output.results.len()),
        )
        .with_sources(dedup_strings(sources))
        .with_code_citations(code_citations_from_search_results(&output.results)?)
        .with_degradations(output.degradations))
    }
}

fn code_citations_from_search_results(
    results: &[crate::output::SearchResultOutput],
) -> Result<Vec<ResearchCodeCitation>, WikiError> {
    let mut citations = Vec::new();
    for hit in results {
        if !hit.result_type.is_code() {
            continue;
        }
        let provenance = if hit.sources.is_empty() {
            vec!["search".to_string()]
        } else {
            hit.sources.clone()
        };
        citations.push(ResearchCodeCitation::new(
            hit.source_path.display().to_string(),
            None,
            hit.title.clone(),
            provenance,
        )?);
    }
    Ok(dedup_code_citations(citations))
}

pub(crate) struct CommandRead {
    pub(crate) selection: ScopeSelection,
}

impl WikiRead for CommandRead {
    fn read(&mut self, path: &Path) -> Result<ResearchObservation, WikiError> {
        let outcome = read::execute(ReadTarget::Path(path.to_path_buf()), self.selection.clone())?;
        let mut observation = observation_from_outcome("read", &outcome);
        observation.sources.push(path.display().to_string());
        observation.sources = dedup_strings(observation.sources);
        Ok(observation)
    }
}

pub(crate) struct CommandIngestor {
    pub(crate) selection: ScopeSelection,
}

impl SourceIngestor for CommandIngestor {
    fn ingest_url(&mut self, url: &str) -> Result<ResearchObservation, WikiError> {
        let outcome = index::execute_ingest_url(vec![url.to_string()], self.selection.clone())?;
        let mut observation = observation_from_outcome("ingest_url", &outcome);
        if outcome.exit_code == 0 && !observation.sources.iter().any(|source| source == url) {
            observation.sources.push(url.to_string());
        }
        observation.sources = dedup_strings(observation.sources);
        Ok(observation)
    }

    fn ingest_file(&mut self, path: &Path) -> Result<ResearchObservation, WikiError> {
        let outcome = index::execute_ingest_file(
            path.to_path_buf(),
            self.selection.clone(),
            IngestFileOptions::default(),
        )?;
        let mut observation = observation_from_outcome("ingest_file", &outcome);
        let path_string = path.display().to_string();
        if outcome.exit_code == 0
            && !observation
                .sources
                .iter()
                .any(|source| source == &path_string)
        {
            observation.sources.push(path_string);
        }
        observation.sources = dedup_strings(observation.sources);
        Ok(observation)
    }
}

pub(crate) struct AcceptedNoteWriter<'a> {
    pub(crate) root: &'a Path,
    pub(crate) session_id: &'a str,
}

impl ResearchNoteWriter for AcceptedNoteWriter<'_> {
    fn write_note(&mut self, note: &AcceptedNoteDraft) -> Result<NoteWriteOutcome, WikiError> {
        let accepted = write_accepted_note(self.root, self.session_id, note)?;
        Ok(NoteWriteOutcome {
            note: accepted.note,
            created: accepted.created,
            write_conflict: accepted.write_conflict,
        })
    }
}
