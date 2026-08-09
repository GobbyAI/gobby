use crate::search::fts;

use super::{CodewikiFacts, SymbolFact};

/// Exact-first indexed symbol search request.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SearchQuery {
    pub text: String,
    pub limit: usize,
    pub kind: Option<String>,
    pub language: Option<String>,
    pub paths: Vec<String>,
}

impl SearchQuery {
    pub fn new(text: impl Into<String>, limit: usize) -> Self {
        Self {
            text: text.into(),
            limit,
            kind: None,
            language: None,
            paths: Vec::new(),
        }
    }
}

/// Owned search hit.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SearchHit {
    pub symbol: SymbolFact,
}

impl CodewikiFacts {
    pub fn search(&self, query: &str, limit: usize) -> anyhow::Result<Vec<SearchHit>> {
        self.search_with(&SearchQuery::new(query, limit))
    }

    pub fn search_with(&self, query: &SearchQuery) -> anyhow::Result<Vec<SearchHit>> {
        let mut conn = self.read_connection()?;
        Ok(fts::search_symbols_exact_first_visible(
            &mut conn,
            &query.text,
            self.context(),
            query.kind.as_deref(),
            query.language.as_deref(),
            &query.paths,
            query.limit,
        )?
        .into_iter()
        .map(|symbol| SearchHit {
            symbol: SymbolFact::from(symbol),
        })
        .collect())
    }
}
