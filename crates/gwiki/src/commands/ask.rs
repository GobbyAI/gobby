mod assembly;
mod citation;
mod dedup;
mod graph_enrichment;
mod narration;
mod render;
mod synthesis;

#[cfg(test)]
mod test_support;

use gobby_core::config::AiRouting;

use crate::commands::search;
use crate::{CommandOutcome, ScopeSelection, WikiError};

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
    let mut output = assembly::ask_output_from_search(search);
    graph_enrichment::enrich_with_attached_unified_graph_context(&mut output, &selection)?;
    if llm {
        synthesis::synthesize(&mut output, ai, require_ai)?;
    }
    render::render(output)
}

#[cfg(test)]
mod tests {
    use super::*;

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
