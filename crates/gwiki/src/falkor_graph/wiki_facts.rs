use std::path::PathBuf;

use postgres::Client;

use crate::WikiError;
use crate::graph::{WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphSource};
use crate::search::SearchScope;
use crate::support::graph::{graph_target_maps, resolve_graph_target};

pub(crate) fn load_wiki_graph_facts(
    conn: &mut Client,
    scope: &SearchScope,
) -> Result<WikiGraphFacts, WikiError> {
    let scope_kind = scope.scope_kind().to_string();
    let scope_id = scope.scope_value().to_string();
    let document_rows = conn
        .query(
            "SELECT path, title, body
             FROM gwiki_documents
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path",
            &[&scope_kind, &scope_id],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load gwiki documents for FalkorDB sync: {error}"),
        })?;
    let document_records = document_rows
        .into_iter()
        .map(|row| {
            (
                PathBuf::from(row.get::<_, String>("path")),
                row.get::<_, Option<String>>("title"),
                row.get::<_, Option<String>>("body").unwrap_or_default(),
            )
        })
        .collect::<Vec<_>>();

    let targets = graph_target_maps(
        document_records
            .iter()
            .map(|(path, title, body)| (path, title.as_deref(), body.as_str())),
    );
    let documents = document_records
        .into_iter()
        .map(|(path, title, _)| WikiGraphDocument {
            scope: scope.clone(),
            path,
            title,
        })
        .collect::<Vec<_>>();

    let link_rows = conn
        .query(
            "SELECT path, target_path
             FROM gwiki_links
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path, target_path",
            &[&scope_kind, &scope_id],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load gwiki links for FalkorDB sync: {error}"),
        })?;
    let links = link_rows
        .into_iter()
        .filter_map(|row| {
            let source_path = PathBuf::from(row.get::<_, String>("path"));
            let raw_target = row.get::<_, String>("target_path");
            resolve_graph_target(&raw_target, &source_path, &targets).map(|target| WikiGraphLink {
                scope: scope.clone(),
                source_path,
                raw_target,
                target,
            })
        })
        .collect::<Vec<_>>();

    let source_rows = conn
        .query(
            "SELECT path, document_path
             FROM gwiki_sources
             WHERE scope_kind = $1 AND scope_id = $2
             ORDER BY path, document_path",
            &[&scope_kind, &scope_id],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load gwiki sources for FalkorDB sync: {error}"),
        })?;
    let sources = source_rows
        .into_iter()
        .map(|row| WikiGraphSource {
            scope: scope.clone(),
            source_path: PathBuf::from(row.get::<_, String>("path")),
            document_path: PathBuf::from(row.get::<_, String>("document_path")),
        })
        .collect::<Vec<_>>();

    Ok(WikiGraphFacts {
        documents,
        links,
        sources,
        code_edges: Vec::new(),
    })
}
