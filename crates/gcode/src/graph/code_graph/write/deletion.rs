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
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->(n {project: $project})
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:INHERITS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:EXTENDS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:IMPLEMENTS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
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
    content_hash: &str,
    sync_token: &str,
) -> anyhow::Result<Vec<TypedQuery>> {
    let base_params = || {
        vec![
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
            ("content_hash", TypedValue::String(content_hash.to_string())),
            sync_token_param(sync_token),
        ]
    };
    let mut queries = vec![
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:IMPORTS]->(:CodeModule {project: $project})
             WHERE r.content_hash = $content_hash
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:DEFINES]->(:CodeSymbol {project: $project})
             WHERE r.content_hash = $content_hash
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->(n {project: $project})
             WHERE r.content_hash = $content_hash
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})
             WHERE r.content_hash = $content_hash
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:INHERITS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND r.content_hash = $content_hash
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:EXTENDS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND r.content_hash = $content_hash
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:IMPLEMENTS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND r.content_hash = $content_hash
               AND (r.sync_token IS NULL OR r.sync_token <> $sync_token)
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
    ];

    // Token-only stale delete: every current symbol was just written with the
    // new sync_token, so a token mismatch alone identifies stale rows. Dropping
    // the per-file symbol-id list keeps the sync request bounded (gobby-cli #678).
    queries.push(typed_query(
        "MATCH (s:CodeSymbol {
            project: $project,
            file_path: $file_path,
            file_content_hash: $content_hash
         })
         WHERE s.sync_token IS NULL OR s.sync_token <> $sync_token
         DETACH DELETE s",
        base_params(),
    )?);

    Ok(queries)
}

pub(crate) fn delete_content_version_queries(
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<Vec<TypedQuery>> {
    let base_params = || {
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
            ("content_hash", TypedValue::String(content_hash.to_string())),
        ]
    };
    Ok(vec![
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:IMPORTS]->(:CodeModule {project: $project})
             WHERE r.content_hash = $content_hash
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (f:CodeFile {path: $file_path, project: $project})-[r:DEFINES]->(:CodeSymbol {project: $project})
             WHERE r.content_hash = $content_hash
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->(n {project: $project})
             WHERE r.content_hash = $content_hash
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:INHERITS|EXTENDS|IMPLEMENTS]->(n {project: $project})
             WHERE r.content_hash = $content_hash
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:INHERITS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND r.content_hash = $content_hash
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:EXTENDS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND r.content_hash = $content_hash
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH ()-[r:IMPLEMENTS]->()
             WHERE r.source_file_path = $file_path
             WITH r, startNode(r) AS s, endNode(r) AS n
             WHERE s.project = $project
               AND n.project = $project
               AND r.content_hash = $content_hash
               AND (s.file_path IS NULL OR s.file_path <> $file_path)
             DELETE r",
            base_params(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {
                project: $project,
                file_path: $file_path,
                file_content_hash: $content_hash
             })
             DETACH DELETE s",
            base_params(),
        )?,
    ])
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

pub(crate) fn delete_empty_file_node_query(
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<TypedQuery> {
    typed_query(
        "MATCH (f:CodeFile {path: $file_path, project: $project})
         WHERE NOT (f)--()
         DELETE f",
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
        ],
    )
}

pub(super) fn retirement_scope_queries(
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<TypedQuery>> {
    let params = || {
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
        ]
    };
    [
        "MATCH (s:CodeSymbol {project:$project, file_path:$file_path})
         RETURN collect(DISTINCT {symbol_id:s.id,content_hash:s.file_content_hash}) AS identities",
        "MATCH (s:CodeFile {project:$project, path:$file_path})-[r]->(n {project:$project})
         WHERE type(r) IN ['DEFINES','IMPORTS']
         RETURN collect(DISTINCT {content_hash:r.content_hash}) AS identities
         UNION ALL
         MATCH (s:CodeSymbol {project:$project, file_path:$file_path})-[r]->(n {project:$project})
         WHERE type(r) IN ['CALLS','INHERITS','EXTENDS','IMPLEMENTS']
         RETURN collect(DISTINCT {content_hash:r.content_hash}) AS identities
         UNION ALL
         MATCH ()-[r:INHERITS]->() WHERE r.source_file_path=$file_path
         WITH r, startNode(r) AS s, endNode(r) AS n
         WHERE s.project=$project AND n.project=$project
         RETURN collect(DISTINCT {content_hash:r.content_hash}) AS identities
         UNION ALL
         MATCH ()-[r:EXTENDS]->() WHERE r.source_file_path=$file_path
         WITH r, startNode(r) AS s, endNode(r) AS n
         WHERE s.project=$project AND n.project=$project
         RETURN collect(DISTINCT {content_hash:r.content_hash}) AS identities
         UNION ALL
         MATCH ()-[r:IMPLEMENTS]->() WHERE r.source_file_path=$file_path
         WITH r, startNode(r) AS s, endNode(r) AS n
         WHERE s.project=$project AND n.project=$project
         RETURN collect(DISTINCT {content_hash:r.content_hash}) AS identities",
        "MATCH (s:CodeFile {project:$project, path:$file_path})-[r]-(n)
         RETURN DISTINCT type(r) AS relationship_type, n.project AS other_project
         UNION
         MATCH (s:CodeSymbol {project:$project, file_path:$file_path})-[r]-(n)
         RETURN DISTINCT type(r) AS relationship_type, n.project AS other_project",
    ]
    .into_iter()
    .map(|cypher| typed_query(cypher, params()))
    .collect()
}

/// One arbitrary-edge scan per bounded batch keeps unknown relationship types
/// visible without repeating a full graph scan for every missing file.
pub(super) fn retirement_detached_query(
    project_id: &str,
    file_paths: &[String],
) -> anyhow::Result<TypedQuery> {
    typed_query(
        "MATCH ()-[r]->() WHERE r.source_file_path IN $file_paths
         WITH r, startNode(r) AS s, endNode(r) AS n
         WHERE (s.project=$project OR n.project=$project)
           AND NOT ((s:CodeFile AND coalesce(s.path,'')=r.source_file_path)
                 OR (s:CodeSymbol AND coalesce(s.file_path,'')=r.source_file_path))
         RETURN r.source_file_path AS file_path,
                collect(DISTINCT {detached_type:type(r),content_hash:r.content_hash,
                  source_project:s.project,target_project:n.project}) AS identities",
        [
            ("project", TypedValue::String(project_id.to_string())),
            (
                "file_paths",
                TypedValue::List(file_paths.iter().cloned().map(TypedValue::String).collect()),
            ),
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
        "MATCH (f:CodeFile {project:$project, path:$file_path}) RETURN count(f) AS nodes
         UNION ALL
         MATCH (s:CodeSymbol {project:$project, file_path:$file_path}) RETURN count(s) AS nodes",
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
        ],
    )
}

/// Remove the file node only when no facts anchor to it any more. A file node
/// still holding DEFINES/IMPORTS edges (e.g. another machine's content hash)
/// must survive a no-fact sync from this machine.
pub(crate) fn delete_bare_file_node_query(
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<TypedQuery> {
    typed_query(
        "MATCH (f:CodeFile {path: $file_path, project: $project})
         WHERE NOT (f)-[:DEFINES]->(:CodeSymbol {project: $project})
           AND NOT (f)-[:IMPORTS]->(:CodeModule {project: $project})
         DETACH DELETE f",
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
               AND NOT ({project: $project})-[:INHERITS|EXTENDS|IMPLEMENTS]->(n)
               AND NOT (n)-[:INHERITS|EXTENDS|IMPLEMENTS]->({project: $project})
             DETACH DELETE n",
        "MATCH (s:CodeSymbol {project: $project})
             WHERE s.file_path IS NULL
               AND NOT (:CodeFile {project: $project})-[:DEFINES]->(s)
               AND NOT ({project: $project})-[:CALLS]->(s)
               AND NOT (s)-[:CALLS]->({project: $project})
               AND NOT ({project: $project})-[:INHERITS|EXTENDS|IMPLEMENTS]->(s)
               AND NOT (s)-[:INHERITS|EXTENDS|IMPLEMENTS]->({project: $project})
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

#[cfg(test)]
pub(crate) fn project_scopes_query() -> TypedQuery {
    TypedQuery::new(format!(
        "MATCH (n)
         WHERE {PROJECT_NODE_PREDICATE}
           AND n.project IS NOT NULL
         RETURN DISTINCT n.project AS project
         ORDER BY project"
    ))
}

#[cfg(test)]
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
