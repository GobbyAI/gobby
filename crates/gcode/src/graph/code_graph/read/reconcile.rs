use std::collections::{BTreeMap, BTreeSet};

use anyhow::Context as _;
use serde_json::Value;

use crate::config::Context;
use crate::graph::typed_query::{TypedQuery, TypedValue};

use super::super::GraphReadError;
use super::super::connection::with_required_core_graph;

const GRAPH_FILE_HASH_PAGE_SIZE: usize = 5_000;

pub type GraphFileHashes = BTreeMap<String, BTreeSet<String>>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphFileHashRead {
    Available(GraphFileHashes),
    Skipped { reason: String },
}

fn graph_file_hashes_query(project_id: &str, skip: usize) -> anyhow::Result<TypedQuery> {
    Ok(TypedQuery::with_params(
        "MATCH (f:CodeFile {project: $project})-[d:DEFINES]->()
         RETURN f.path AS path, collect(DISTINCT d.content_hash) AS hashes
         ORDER BY path SKIP $skip LIMIT 5000",
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("skip", TypedValue::Integer(i64::try_from(skip)?)),
        ],
    )?)
}

pub fn read_project_file_hashes(ctx: &Context) -> anyhow::Result<GraphFileHashRead> {
    let result = with_required_core_graph(ctx, |client| {
        let mut files = GraphFileHashes::new();
        let mut skip = 0usize;
        loop {
            let query = graph_file_hashes_query(&ctx.project_id, skip)?;
            let rows = client.query(&query.cypher, Some(query.params))?;
            let row_count = rows.len();
            for row in rows {
                let path = row
                    .get("path")
                    .and_then(Value::as_str)
                    .context("graph reconcile row is missing CodeFile path")?;
                let hashes = row
                    .get("hashes")
                    .and_then(Value::as_array)
                    .context("graph reconcile row is missing DEFINES hashes")?;
                files.entry(path.to_string()).or_default().extend(
                    hashes
                        .iter()
                        .filter_map(Value::as_str)
                        .map(ToOwned::to_owned),
                );
            }
            if row_count < GRAPH_FILE_HASH_PAGE_SIZE {
                break;
            }
            skip = skip.saturating_add(GRAPH_FILE_HASH_PAGE_SIZE);
        }
        Ok(files)
    });

    match result {
        Ok(files) => Ok(GraphFileHashRead::Available(files)),
        Err(error)
            if matches!(
                error.downcast_ref::<GraphReadError>(),
                Some(GraphReadError::NotConfigured | GraphReadError::Unreachable { .. })
            ) =>
        {
            Ok(GraphFileHashRead::Skipped {
                reason: error.to_string(),
            })
        }
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reconcile_query_is_project_scoped_and_resultset_bounded() {
        let query = graph_file_hashes_query("project-1", 5_000).expect("query");
        assert!(query.cypher.contains("CodeFile {project: $project}"));
        assert!(query.cypher.contains("collect(DISTINCT d.content_hash)"));
        assert!(query.cypher.contains("SKIP $skip LIMIT 5000"));
        assert_eq!(
            query.params.get("project").map(String::as_str),
            Some("'project-1'")
        );
        assert_eq!(query.params.get("skip").map(String::as_str), Some("5000"));
    }
}
