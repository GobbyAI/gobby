use std::collections::{BTreeMap, BTreeSet};

use regex::Regex;

use crate::codewiki_facts::{GrepQuery, ScopeSelector, SearchQuery, SymbolFact};

use super::contracts::{Completeness, EvidenceItem, EvidenceWarning, SearchLane, SearchSelector};
use super::read::{source_for_lines, source_for_symbol};
use super::source::validate_repo_path;
use super::{EvidenceError, EvidenceLibrary, QueryResult, Result};

pub(super) fn execute(library: &EvidenceLibrary, selector: &SearchSelector) -> Result<QueryResult> {
    validate_selector(selector)?;
    let eligible = scoped_files(library, selector);
    let (mut items, truncated, hybrid) = match selector.lane {
        SearchLane::Symbol | SearchLane::LexicalSymbol => {
            let page = symbol_search(library, selector)?;
            let exact = selector.lane == SearchLane::Symbol;
            let symbols = page
                .items
                .into_iter()
                .filter(|symbol| {
                    !exact
                        || symbol.name == selector.query
                        || symbol.qualified_name == selector.query
                })
                .collect::<Vec<_>>();
            (symbols_to_items(library, symbols)?, page.truncated, None)
        }
        SearchLane::Literal | SearchLane::Regex => {
            let (items, truncated) = grep_search(library, selector, &eligible)?;
            (items, truncated, None)
        }
        SearchLane::Content => {
            let (items, truncated) = content_search(library, selector)?;
            (items, truncated, None)
        }
        SearchLane::Hybrid => hybrid_search(library, selector)?,
    };
    items.retain(|item| match item {
        EvidenceItem::Source(source) => eligible.contains(&source.path),
        EvidenceItem::Graph(_) | EvidenceItem::CommitMetadata(_) => false,
    });
    let mut warnings = Vec::new();
    let completeness = if truncated {
        Completeness::TruncatedIndex
    } else if items.is_empty() {
        warnings.push(EvidenceWarning {
            code: "search_absence_not_repository_negative".to_string(),
            message: "a complete empty search result does not prove a repository-wide negative"
                .to_string(),
            path: None,
        });
        Completeness::CompleteEmpty
    } else {
        Completeness::Complete
    };
    Ok(QueryResult {
        items,
        completeness,
        lane: lane_name(selector.lane).to_string(),
        hybrid,
        warnings,
        result_limit: selector.limit,
        graph_depth: None,
    })
}

pub(super) fn validate_selector(selector: &SearchSelector) -> Result<()> {
    if selector.query.trim().is_empty() {
        return Err(EvidenceError::InvalidSelector {
            detail: "search query must not be empty".to_string(),
        });
    }
    if selector.limit == 0 {
        return Err(EvidenceError::InvalidSelector {
            detail: "search limit must be positive".to_string(),
        });
    }
    for path in &selector.paths {
        validate_repo_path(path)?;
    }
    if selector.kind.is_some()
        && !matches!(
            selector.lane,
            SearchLane::Symbol | SearchLane::LexicalSymbol | SearchLane::Hybrid
        )
    {
        return Err(EvidenceError::InvalidSelector {
            detail: "kind filter is valid only for symbol lanes".to_string(),
        });
    }
    match (selector.lane, &selector.hybrid_identity) {
        (SearchLane::Hybrid, None) => return Err(EvidenceError::SemanticIdentityRequired),
        (SearchLane::Hybrid, Some(identity))
            if identity.endpoint.is_empty()
                || identity.model.is_empty()
                || identity.dimension == 0
                || identity.index_id.is_empty() =>
        {
            return Err(EvidenceError::SemanticIdentityRequired);
        }
        (SearchLane::Hybrid, Some(_)) => {}
        (_, Some(_)) => {
            return Err(EvidenceError::InvalidSelector {
                detail: "hybrid identity is incompatible with a deterministic lane".to_string(),
            });
        }
        (_, None) => {}
    }
    if selector.lane == SearchLane::Regex {
        Regex::new(&selector.query).map_err(|error| EvidenceError::InvalidSelector {
            detail: format!("invalid regular expression: {error}"),
        })?;
    }
    Ok(())
}

fn scoped_files(library: &EvidenceLibrary, selector: &SearchSelector) -> BTreeSet<String> {
    library
        .files
        .values()
        .filter(|entry| {
            matches_paths(&entry.path, &selector.paths)
                && selector
                    .language
                    .as_ref()
                    .is_none_or(|language| &entry.language == language)
        })
        .map(|entry| entry.path.clone())
        .collect()
}

fn matches_paths(path: &str, selectors: &[String]) -> bool {
    selectors.is_empty()
        || selectors.iter().any(|selector| {
            path == selector
                || path
                    .strip_prefix(selector.trim_end_matches('/'))
                    .is_some_and(|suffix| suffix.starts_with('/'))
        })
}

fn symbol_search(
    library: &EvidenceLibrary,
    selector: &SearchSelector,
) -> Result<super::FactPage<SymbolFact>> {
    let mut query = SearchQuery::new(&selector.query, selector.limit);
    query.kind.clone_from(&selector.kind);
    query.language.clone_from(&selector.language);
    // Evidence scopes are file or directory prefixes, but the symbol index
    // post-filters by glob; expand them as the search commands do.
    query.paths = crate::search::fts::expand_paths(&selector.paths);
    library
        .facts
        .search_symbols(&query)
        .map_err(|error| EvidenceError::IndexUnavailable {
            reason: format!("{error:#}"),
        })
}

fn symbols_to_items(
    library: &EvidenceLibrary,
    symbols: Vec<SymbolFact>,
) -> Result<Vec<EvidenceItem>> {
    symbols
        .into_iter()
        .map(|symbol| source_for_symbol(library, &symbol).map(EvidenceItem::Source))
        .collect()
}

fn content_search(
    library: &EvidenceLibrary,
    selector: &SearchSelector,
) -> Result<(Vec<EvidenceItem>, bool)> {
    let mut query = SearchQuery::new(&selector.query, selector.limit);
    query.language.clone_from(&selector.language);
    query.paths.clone_from(&selector.paths);
    let page =
        library
            .facts
            .search_content(&query)
            .map_err(|error| EvidenceError::IndexUnavailable {
                reason: format!("{error:#}"),
            })?;
    let mut seen = BTreeSet::new();
    let mut items = Vec::new();
    for hit in page.items {
        if !seen.insert((hit.path.clone(), hit.line_start, hit.line_end)) {
            continue;
        }
        items.push(EvidenceItem::Source(source_for_lines(
            library,
            &hit.path,
            hit.line_start,
            hit.line_end,
            None,
        )?));
    }
    Ok((items, page.truncated))
}

fn grep_search(
    library: &EvidenceLibrary,
    selector: &SearchSelector,
    eligible: &BTreeSet<String>,
) -> Result<(Vec<EvidenceItem>, bool)> {
    let scope = if selector.language.is_some() {
        ScopeSelector::paths(eligible.iter().cloned())
    } else if selector.paths.is_empty() {
        ScopeSelector::all()
    } else {
        ScopeSelector::paths(selector.paths.clone())
    };
    let mut query = GrepQuery::new(&selector.query, scope);
    query.fixed_strings = selector.lane == SearchLane::Literal;
    query.limit = selector.limit;
    let outcome = library
        .facts
        .grep(&query)
        .map_err(|error| EvidenceError::IndexUnavailable {
            reason: format!("{error:#}"),
        })?;
    let mut seen = BTreeSet::new();
    let mut items = Vec::new();
    for hit in outcome.hits {
        if !seen.insert((hit.path.clone(), hit.line)) {
            continue;
        }
        items.push(EvidenceItem::Source(source_for_lines(
            library, &hit.path, hit.line, hit.line, None,
        )?));
    }
    Ok((items, outcome.truncated))
}

fn hybrid_search(
    library: &EvidenceLibrary,
    selector: &SearchSelector,
) -> Result<(Vec<EvidenceItem>, bool, Option<super::HybridIdentity>)> {
    let expected = selector
        .hybrid_identity
        .as_ref()
        .ok_or(EvidenceError::SemanticIdentityRequired)?;
    let provider = library
        .hybrid
        .as_ref()
        .ok_or_else(|| EvidenceError::SemanticFailure {
            reason:
                "hybrid evidence is unavailable because no audited semantic provider is configured"
                    .to_string(),
        })?;
    let effective = provider
        .effective_identity()
        .map_err(|reason| EvidenceError::SemanticFailure { reason })?;
    if effective != *expected {
        return Err(EvidenceError::SemanticIdentityMismatch {
            expected: Box::new(expected.clone()),
            found: Box::new(effective),
        });
    }
    let lexical = symbol_search(library, selector)?;
    let semantic = provider
        .search_symbol_ids(selector)
        .map_err(|reason| EvidenceError::SemanticFailure { reason })?;
    let lexical_ids = lexical
        .items
        .iter()
        .map(|symbol| symbol.id.clone())
        .collect::<Vec<_>>();
    let lexical_by_id = lexical
        .items
        .into_iter()
        .map(|symbol| (symbol.id.clone(), symbol))
        .collect::<BTreeMap<_, _>>();
    let merged = crate::search::rrf::merge(vec![
        ("lexical_symbol", lexical_ids),
        ("semantic", semantic.items),
    ]);
    let mut symbols = Vec::new();
    for (id, _, _) in merged {
        let symbol = match lexical_by_id.get(&id) {
            Some(symbol) => Some(symbol.clone()),
            None => library.facts.symbol_by_id(&id).map_err(|error| {
                EvidenceError::IndexUnavailable {
                    reason: format!("{error:#}"),
                }
            })?,
        };
        let Some(symbol) = symbol else {
            return Err(EvidenceError::FactMismatch {
                path: "<semantic-index>".to_string(),
                expected: "visible pinned symbol".to_string(),
                found: id,
            });
        };
        if selector
            .kind
            .as_ref()
            .is_none_or(|kind| symbol.kind == *kind)
            && selector
                .language
                .as_ref()
                .is_none_or(|language| symbol.language == *language)
            && matches_paths(&symbol.file_path, &selector.paths)
        {
            symbols.push(symbol);
        }
    }
    let union_truncated = symbols.len() > selector.limit;
    symbols.truncate(selector.limit);
    Ok((
        symbols_to_items(library, symbols)?,
        lexical.truncated || semantic.truncated || union_truncated,
        Some(effective),
    ))
}

fn lane_name(lane: SearchLane) -> &'static str {
    match lane {
        SearchLane::Symbol => "symbol",
        SearchLane::Literal => "literal",
        SearchLane::Regex => "regex",
        SearchLane::Content => "content",
        SearchLane::LexicalSymbol => "lexical_symbol",
        SearchLane::Hybrid => "hybrid",
    }
}
