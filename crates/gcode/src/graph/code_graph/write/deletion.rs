use crate::graph::typed_query::{TypedQuery, TypedValue};

use super::support::{sync_token_param, typed_query};

const PROJECT_NODE_PREDICATE: &str =
    "n:CodeFile OR n:CodeSymbol OR n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol";

pub(crate) fn delete_file_graph_queries(
    project_id: &str,
    file_path: &str,
    current_symbol_ids: &[String],
) -> anyhow::Result<Vec<TypedQuery>> {
    let base_params = || {
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
        ]
    };
    let mut queries = vec![
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:IMPORTS]->(:CodeModule)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:DEFINES]->(:CodeSymbol)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->()
             DELETE r",
            base_params(),
        )?,
    ];

    if current_symbol_ids.is_empty() {
        queries.push(typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
             DETACH DELETE s",
            base_params(),
        )?);
    } else {
        let mut params = vec![
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
            (
                "symbol_ids",
                TypedValue::List(
                    current_symbol_ids
                        .iter()
                        .map(|id| TypedValue::String(id.clone()))
                        .collect(),
                ),
            ),
        ];
        queries.push(typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
             WHERE NOT s.id IN $symbol_ids
             DETACH DELETE s",
            params.drain(..),
        )?);
    }

    Ok(queries)
}

pub(crate) fn delete_stale_file_graph_queries(
    project_id: &str,
    file_path: &str,
    sync_token: &str,
) -> anyhow::Result<Vec<TypedQuery>> {
    let base_params = || {
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
            sync_token_param(sync_token),
        ]
    };
    let mut queries = vec![
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:IMPORTS]->(:CodeModule {project: $project})
             WHERE r.sync_token IS NULL OR r.sync_token <> $sync_token
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:DEFINES]->(:CodeSymbol {project: $project})
             WHERE r.sync_token IS NULL OR r.sync_token <> $sync_token
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project})-[r:CALLS]->(n {project: $project})
             WHERE (r.file = $file_path OR r.source_file_path = $file_path)
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
             DELETE r",
            base_params(),
        )?,
    ];

    // Token-only stale delete: every current symbol was just written with the
    // new sync_token, so a token mismatch alone identifies stale rows. Dropping
    // the per-file symbol-id list keeps the sync request bounded (gobby-cli #678).
    queries.push(typed_query(
        "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
         WHERE s.sync_token IS NULL OR s.sync_token <> $sync_token
         DETACH DELETE s",
        base_params(),
    )?);

    Ok(queries)
}

pub(crate) fn delete_file_node_query(
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<TypedQuery> {
    typed_query(
        "MATCH (f:CodeFile {path: $file_path, project: $project})
         DETACH DELETE f",
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
        ],
    )
}

pub(crate) fn project_file_path_queries(project_id: &str) -> anyhow::Result<Vec<TypedQuery>> {
    let project_param = || [("project", TypedValue::String(project_id.to_string()))];
    Ok(vec![
        typed_query(
            "MATCH (f:CodeFile {project: $project})
             WHERE f.path IS NOT NULL
             RETURN DISTINCT f.path AS path",
            project_param(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project})
             WHERE s.file_path IS NOT NULL
             RETURN DISTINCT s.file_path AS path",
            project_param(),
        )?,
    ])
}

pub(crate) fn count_file_projection_nodes_query(
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<TypedQuery> {
    typed_query(
        "MATCH (n {project: $project})
         WHERE (n:CodeFile AND n.path = $file_path)
            OR (n:CodeSymbol AND n.file_path = $file_path)
         RETURN count(n) AS nodes",
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
        ],
    )
}

pub(crate) fn cleanup_orphans_queries(project_id: &str) -> anyhow::Result<Vec<TypedQuery>> {
    let project_param = || [("project", TypedValue::String(project_id.to_string()))];
    // Orphan cleanup runs after low-activity sync paths so failed writes leave
    // the previous projection available.
    cleanup_orphans_cypher_segments()
        .into_iter()
        .map(|cypher| typed_query(cypher, project_param()))
        .collect()
}

fn cleanup_orphans_cypher_segments() -> [&'static str; 3] {
    [
        "MATCH (m:CodeModule {project: $project})
             WHERE NOT (:CodeFile {project: $project})-[:IMPORTS]->(m)
             DETACH DELETE m",
        "MATCH (n {project: $project})
             WHERE (n:UnresolvedCallee OR n:ExternalSymbol)
               AND NOT ({project: $project})-[:CALLS]->(n)
             DETACH DELETE n",
        "MATCH (s:CodeSymbol {project: $project})
             WHERE s.file_path IS NULL
               AND NOT (:CodeFile {project: $project})-[:DEFINES]->(s)
               AND NOT ({project: $project})-[:CALLS]->(s)
               AND NOT (s)-[:CALLS]->({project: $project})
             DETACH DELETE s",
    ]
}

pub(crate) fn clear_project_query(project_id: &str) -> anyhow::Result<TypedQuery> {
    typed_query(
        format!(
            "MATCH (n {{project: $project}})
             WHERE {PROJECT_NODE_PREDICATE}
             DETACH DELETE n"
        ),
        [("project", TypedValue::String(project_id.to_string()))],
    )
}

pub(crate) fn clear_all_code_index_query() -> anyhow::Result<TypedQuery> {
    typed_query(
        format!(
            "MATCH (n)
             WHERE {PROJECT_NODE_PREDICATE}
             DETACH DELETE n"
        ),
        Vec::<(&str, TypedValue)>::new(),
    )
}
