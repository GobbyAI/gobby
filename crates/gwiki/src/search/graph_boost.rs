use std::path::PathBuf;

use crate::search::{
    SearchHitKind, SearchProvenance, SearchScope, SearchSource, WikiSearchResult,
    bm25::is_keyword_searchable_path,
};

pub fn graph_boost_hits(
    scope: SearchScope,
    ranked_paths: Vec<(PathBuf, f64)>,
    limit: usize,
) -> Vec<WikiSearchResult> {
    ranked_paths
        .into_iter()
        .filter(|(path, _)| is_keyword_searchable_path(&path.to_string_lossy()))
        .take(limit)
        .map(|(path, score)| graph_result(&scope, path, score))
        .collect()
}

fn graph_result(scope: &SearchScope, path: PathBuf, score: f64) -> WikiSearchResult {
    let id = format!("document:{}", path.to_string_lossy().replace('\\', "/"));
    let provenance = SearchProvenance {
        document_path: path.clone(),
        source_path: path.clone(),
        source_kind: "graph".to_string(),
        content_hash: None,
    };
    WikiSearchResult {
        id,
        title: None,
        scope: scope.clone(),
        source_path: path.clone(),
        path,
        hit_kind: SearchHitKind::Document,
        snippet: String::new(),
        score,
        sources: vec![SearchSource::Graph],
        explanations: Vec::new(),
        chunk: None,
        provenance,
    }
}
