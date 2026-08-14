use std::path::PathBuf;

use serde_json::json;

use crate::graph::{LinkSuggestion, MemoryWikiGraph, WikiBacklink};
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

fn graph_for_selection(
    selection: &ScopeSelection,
    command: &'static str,
) -> Result<(ScopeIdentity, crate::search::SearchScope, MemoryWikiGraph), WikiError> {
    let resolved = resolve_selection_context(selection)?;
    let database_url = database_url_for(command)?
        .ok_or_else(|| WikiError::from(gobby_core::grant::GrantError::DaemonRequired))?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
        }
    })?;
    let facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    let mut graph = MemoryWikiGraph::default();
    graph.replace_facts(facts);
    Ok((resolved.output_scope, resolved.search_scope, graph))
}

pub(crate) fn execute(
    page: String,
    selection: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let (output_scope, search_scope, graph) = graph_for_selection(&selection, "gwiki backlinks")?;
    let backlinks = graph.backlinks(&search_scope, PathBuf::from(&page));
    Ok(render_backlinks(&page, output_scope, &backlinks))
}

pub(crate) fn execute_link_suggest(
    selection: ScopeSelection,
    limit: usize,
) -> Result<CommandOutcome, WikiError> {
    let (output_scope, search_scope, graph) =
        graph_for_selection(&selection, "gwiki link-suggest")?;
    let suggestions = graph.link_suggestions(&search_scope, limit);
    Ok(render_link_suggest(output_scope, limit, &suggestions))
}

fn render_backlinks(
    page: &str,
    scope: ScopeIdentity,
    backlinks: &[WikiBacklink],
) -> CommandOutcome {
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

fn render_link_suggest(
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
                "variants": &suggestion.variants,
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
    let mut text = format!(
        "Backlinks for {page}
Scope: {scope}
"
    );
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
    let mut text = format!(
        "Link suggestions
Scope: {scope}
"
    );
    if suggestions.is_empty() {
        text.push_str("No suggestions");
        return text;
    }

    for suggestion in suggestions {
        text.push_str("- ");
        text.push_str(&suggestion.target);
        text.push_str(" (");
        text.push_str(&suggestion.mention_count.to_string());
        text.push(' ');
        text.push_str(if suggestion.mention_count == 1 {
            "mention"
        } else {
            "mentions"
        });
        if suggestion.variants.len() > 1 {
            text.push_str("; variants: ");
            text.push_str(&suggestion.variants.join(", "));
        }
        text.push_str(")\n");
    }
    text
}
