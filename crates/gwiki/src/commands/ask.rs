mod assembly;
mod citation;
mod evidence;
mod narration;
mod render;
mod synthesis;

#[cfg(test)]
mod test_support;

use gobby_core::config::AiRouting;

use crate::commands::search;
use crate::{CommandOutcome, ScopeSelection, WikiError};

const DEFAULT_ASK_HIT_LIMIT: usize = 10;

/// Thin RAG over `search`: top-k retrieval, a bounded evidence prompt, one
/// completion, grounded citations. No whole-scope context expansion.
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

    let retrieval = search::retrieve(query, selection, DEFAULT_ASK_HIT_LIMIT, true)?;
    let plan = evidence::plan_evidence(&retrieval);
    let mut output = assembly::ask_output_from_retrieval(retrieval.output, &plan);
    if llm {
        synthesis::synthesize(&mut output, &plan, ai, require_ai)?;
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
