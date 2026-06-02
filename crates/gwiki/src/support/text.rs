use std::path::Path;

use gobby_core::degradation::{DegradationKind, ServiceState};

use crate::{setup, store};

pub(crate) fn query_tokens(query: &str) -> Vec<String> {
    query
        .split(|ch: char| !ch.is_alphanumeric())
        .filter(|token| !token.is_empty())
        .map(str::to_lowercase)
        .collect()
}

pub(crate) fn keyword_score(text: &str, tokens: &[String]) -> usize {
    let haystack = text.to_lowercase();
    // Contract: `tokens` come from `query_tokens` and are already normalized.
    tokens
        .iter()
        .map(|token| haystack.matches(token).count())
        .sum()
}

pub(crate) fn snippet_from_text(text: &str) -> String {
    let snippet = text
        .lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or_default()
        .trim();
    if snippet.chars().count() <= 240 {
        return snippet.to_string();
    }

    format!("{}...", snippet.chars().take(237).collect::<String>())
}

pub(crate) fn degradation_label(degradation: &DegradationKind) -> String {
    match degradation {
        DegradationKind::ServiceUnavailable { service, state } => match state {
            ServiceState::Available => format!("{service}_available"),
            ServiceState::NotConfigured => format!("{service}_not_configured"),
            ServiceState::Unreachable { .. } => format!("{service}_unreachable"),
        },
        DegradationKind::PartialSearch { .. } => "partial_search".to_string(),
        DegradationKind::StaleIndex { .. } => "stale_index".to_string(),
        DegradationKind::SkippedArtifacts { .. } => "skipped_artifacts".to_string(),
    }
}

pub(crate) fn document_kind_name(kind: store::WikiDocumentKind) -> &'static str {
    match kind {
        store::WikiDocumentKind::SourceCatalog => "source_catalog",
        store::WikiDocumentKind::SourceNote => "source_note",
        store::WikiDocumentKind::Concept => "concept",
        store::WikiDocumentKind::Topic => "topic",
        store::WikiDocumentKind::CodeDoc => "code_doc",
    }
}

pub(crate) fn postgres_object_kind(kind: setup::GwikiPostgresObjectKind) -> &'static str {
    match kind {
        setup::GwikiPostgresObjectKind::Preflight => "preflight",
        setup::GwikiPostgresObjectKind::Table => "table",
        setup::GwikiPostgresObjectKind::Index => "index",
    }
}

pub(crate) fn display_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

pub(crate) fn slugify(value: &str) -> String {
    let mut slug = String::new();
    let mut last_was_dash = false;
    for ch in value.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
    }
    slug.trim_matches('-').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snippets_reserve_space_for_ellipsis() {
        let snippet = snippet_from_text(&"a".repeat(300));

        assert!(snippet.ends_with("..."));
        assert_eq!(snippet.chars().count(), 240);
    }
}
