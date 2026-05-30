use serde_json::json;

use crate::graph::{LinkSuggestion, WikiBacklink};
use crate::{CommandOutcome, ScopeIdentity};

pub(crate) fn run(page: &str, scope: ScopeIdentity, backlinks: &[WikiBacklink]) -> CommandOutcome {
    let backlink_payloads = backlinks
        .iter()
        .map(|backlink| {
            json!({
                "source_path": &backlink.source_path,
                "target_path": &backlink.target_path,
                "raw_target": &backlink.raw_target,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "backlinks",
        "scope": scope,
        "page": page,
        "backlinks": backlink_payloads,
    });
    let text = render_backlinks_text(page, &scope, backlinks);
    super::scoped_outcome("backlinks", &scope, payload, text)
}

pub(crate) fn link_suggest(
    scope: ScopeIdentity,
    limit: usize,
    suggestions: &[LinkSuggestion],
) -> CommandOutcome {
    let suggestion_payloads = suggestions
        .iter()
        .map(|suggestion| {
            json!({
                "target": &suggestion.target,
                "mention_count": suggestion.mention_count,
                "source_paths": &suggestion.source_paths,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "link-suggest",
        "scope": scope,
        "limit": limit,
        "suggestions": suggestion_payloads,
    });
    let text = render_link_suggest_text(&scope, suggestions);
    super::scoped_outcome("link-suggest", &scope, payload, text)
}

fn render_backlinks_text(page: &str, scope: &ScopeIdentity, backlinks: &[WikiBacklink]) -> String {
    let mut text = format!("Backlinks for {page}\nScope: {scope}\n");
    if backlinks.is_empty() {
        text.push_str("No backlinks");
        return text;
    }

    for backlink in backlinks {
        text.push_str("- ");
        text.push_str(&backlink.source_path.display().to_string());
        text.push_str(" via ");
        text.push_str(&backlink.raw_target);
        text.push('\n');
    }
    text
}

fn render_link_suggest_text(scope: &ScopeIdentity, suggestions: &[LinkSuggestion]) -> String {
    let mut text = format!("Link suggestions\nScope: {scope}\n");
    if suggestions.is_empty() {
        text.push_str("No suggestions");
        return text;
    }

    for suggestion in suggestions {
        text.push_str("- ");
        text.push_str(&suggestion.target);
        text.push_str(" (");
        text.push_str(&suggestion.mention_count.to_string());
        text.push_str(" mentions)\n");
    }
    text
}
