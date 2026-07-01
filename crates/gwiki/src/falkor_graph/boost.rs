use std::path::PathBuf;

use gobby_core::degradation::DegradationKind;
use gobby_core::falkor::{GraphClient, Row};

use crate::search::{SearchError, SearchScope};

use super::GraphBoostData;
use super::query::{optional_row_string, row_string, scope_params};

pub(crate) fn load_graph_boost_data(
    client: &mut GraphClient,
    scope: &SearchScope,
    document_limit: i64,
    link_limit: i64,
) -> Result<GraphBoostData, SearchError> {
    let documents = query_documents(client, scope, document_limit)?;
    let links = query_links(client, scope, link_limit)?;
    let mut capped = Vec::new();
    if documents.capped {
        capped.push(format!("documents>{document_limit}"));
    }
    if links.capped {
        capped.push(format!("links>{link_limit}"));
    }
    let degradation = partial_graph_degradation(&capped);
    if let Some(degradation) = &degradation {
        log::warn!("loaded partial FalkorDB wiki graph boost data: {degradation:?}");
    }
    Ok(GraphBoostData {
        documents: documents.items,
        links: links.items,
        degradation,
    })
}

fn query_documents(
    client: &mut GraphClient,
    scope: &SearchScope,
    limit: i64,
) -> Result<LimitedQuery<crate::search::graph_boost::GraphBoostDocument>, SearchError> {
    let query = if matches!(scope, SearchScope::Global) {
        "MATCH (doc:WikiDoc)
         RETURN doc.path AS path, doc.title AS title
         ORDER BY path"
    } else {
        "MATCH (doc:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id})
         RETURN doc.path AS path, doc.title AS title
         ORDER BY path"
    };
    let rows = query_limited(client, scope, query, "document", limit)?;
    let items = rows
        .items
        .into_iter()
        .map(|row| {
            let path = row_string(&row, "path")?;
            Ok(crate::search::graph_boost::GraphBoostDocument {
                path: PathBuf::from(path),
                title: optional_row_string(&row, "title"),
            })
        })
        .collect::<Result<Vec<_>, SearchError>>()?;
    Ok(LimitedQuery {
        items,
        capped: rows.capped,
    })
}

fn query_links(
    client: &mut GraphClient,
    scope: &SearchScope,
    limit: i64,
) -> Result<LimitedQuery<crate::search::graph_boost::GraphBoostLink>, SearchError> {
    let query = if matches!(scope, SearchScope::Global) {
        "MATCH (source:WikiDoc)
             -[:WIKI_LINKS_TO]->
             (target:WikiDoc)
         RETURN source.path AS path, target.path AS target_path
         ORDER BY path, target_path"
    } else {
        "MATCH (source:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id})
             -[:WIKI_LINKS_TO]->
             (target:WikiDoc {scope_kind: $scope_kind, scope_id: $scope_id})
         RETURN source.path AS path, target.path AS target_path
         ORDER BY path, target_path"
    };
    let rows = query_limited(client, scope, query, "link", limit)?;
    let items = rows
        .items
        .into_iter()
        .map(|row| {
            let source_path = row_string(&row, "path")?;
            let target_path = row_string(&row, "target_path")?;
            Ok(crate::search::graph_boost::GraphBoostLink {
                source_path: PathBuf::from(source_path),
                target_path,
            })
        })
        .collect::<Result<Vec<_>, SearchError>>()?;
    Ok(LimitedQuery {
        items,
        capped: rows.capped,
    })
}

struct LimitedQuery<T> {
    items: Vec<T>,
    capped: bool,
}

fn query_limited(
    client: &mut GraphClient,
    scope: &SearchScope,
    base_query: &str,
    component: &'static str,
    limit: i64,
) -> Result<LimitedQuery<Row>, SearchError> {
    let limit = limit.max(0);
    let fetch_limit = usize::try_from(limit)
        .map_err(|_| SearchError::Backend(format!("invalid graph {component} limit {limit}")))?
        .saturating_add(1);
    let query = format!("{base_query}\nLIMIT {fetch_limit}");
    let mut rows = client.query(&query, scope_params(scope)).map_err(|error| {
        SearchError::Backend(format!("query FalkorDB wiki {component}s: {error}"))
    })?;
    let capped = rows.len() == fetch_limit && fetch_limit > 0;
    if let Ok(limit) = usize::try_from(limit)
        && rows.len() > limit
    {
        // Query fetches one sentinel row to detect truncation; drop it before
        // callers build graph facts so the result never exceeds the contract.
        rows.truncate(limit);
    }
    Ok(LimitedQuery {
        items: rows,
        capped,
    })
}

pub(super) fn partial_graph_degradation(capped: &[String]) -> Option<DegradationKind> {
    if capped.is_empty() {
        return None;
    }
    Some(DegradationKind::PartialData {
        component: "gwiki_graph".to_string(),
        message: format!(
            "FalkorDB wiki graph exceeded configured caps ({}); graph boost used capped data",
            capped.join(", ")
        ),
    })
}
