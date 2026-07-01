use crate::commands::ask::citation::CITATION_CHECK_SUPPORTED;
use crate::commands::scoped_outcome;
use crate::output::AskOutput;
use crate::{CommandOutcome, ScopeIdentity, WikiError};

pub(super) fn render(output: AskOutput) -> Result<CommandOutcome, WikiError> {
    let scope = output.scope.clone();
    let text = render_text(&output.query, &scope, &output);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize ask output",
        path: None,
        source: error,
    })?;

    Ok(scoped_outcome("ask", &scope, payload, text))
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::ask::assembly::ask_output_from_retrieval;
    use crate::commands::ask::evidence::plan_evidence;
    use crate::commands::ask::synthesis::record_synthesis;
    use crate::commands::ask::test_support::retrieval_with_hooks_hit;

    #[test]
    fn unverified_synthesis_is_flagged_in_text_render() {
        let retrieval = retrieval_with_hooks_hit();
        let plan = plan_evidence(&retrieval);
        let mut output = ask_output_from_retrieval(retrieval.output, &plan);
        record_synthesis(
            &mut output,
            &plan.excerpts,
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

        let retrieval = retrieval_with_hooks_hit();
        let plan = plan_evidence(&retrieval);
        let mut grounded = ask_output_from_retrieval(retrieval.output, &plan);
        record_synthesis(
            &mut grounded,
            &plan.excerpts,
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
}
