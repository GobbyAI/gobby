mod assembly;
mod citation;
mod deep;
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
#[allow(clippy::too_many_arguments)]
pub(crate) fn execute(
    query: String,
    selection: ScopeSelection,
    llm: bool,
    deep: bool,
    ai: AiRouting,
    token_budget: Option<usize>,
    include_candidates: bool,
) -> Result<CommandOutcome, WikiError> {
    if (llm || deep) && ai == AiRouting::Off {
        let flag = if deep { "--deep" } else { "--llm" };
        return Err(WikiError::InvalidInput {
            field: "ask",
            message: format!("{flag} cannot be combined with --no-ai"),
        });
    }

    let retrieval = search::retrieve(
        query,
        selection.clone(),
        DEFAULT_ASK_HIT_LIMIT,
        true,
        token_budget,
        include_candidates,
    )?;
    let plan = evidence::plan_evidence(&retrieval);
    let mut output = assembly::ask_output_from_retrieval(retrieval.output, &plan);
    if deep {
        deep::synthesize(&mut output, &plan, selection, ai, true)?;
    } else if llm {
        synthesis::synthesize(&mut output, &plan, ai, true)?;
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
            false,
            AiRouting::Off,
            None,
            false,
        )
        .expect_err("ask --llm --no-ai should fail before retrieval");

        assert!(matches!(
            error,
            WikiError::InvalidInput { field: "ask", .. }
        ));
    }

    #[test]
    fn deep_ai_off_is_invalid_input() {
        let error = execute(
            "Question?".to_string(),
            ScopeSelection::detect(),
            false,
            true,
            AiRouting::Off,
            None,
            false,
        )
        .expect_err("ask --deep --no-ai should fail before retrieval");

        assert!(matches!(
            error,
            WikiError::InvalidInput { field: "ask", .. }
        ));
    }
}
