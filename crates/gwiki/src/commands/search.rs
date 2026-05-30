use crate::output::{SearchOutput, SearchResultOutput};
use crate::{CommandOutcome, ScopeIdentity, WikiError};

pub(crate) fn run(output: SearchOutput) -> Result<CommandOutcome, WikiError> {
    let scope = output.scope.clone();
    let query = output.query.clone();
    let text = render_text(&query, &scope, &output.results);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize search output",
        path: None,
        source: error.to_string(),
    })?;

    Ok(super::scoped_outcome("search", &scope, payload, text))
}

fn render_text(query: &str, scope: &ScopeIdentity, results: &[SearchResultOutput]) -> String {
    let mut text = format!("Search results for \"{query}\"\nScope: {scope}\n");
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
