use std::collections::HashMap;
use std::fmt;

use anyhow::Context as _;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::Context;
use crate::graph::typed_query::{self, TypedQuery, TypedValue};
use crate::models::{
    CallRelation, CallTargetKind, GraphResult, ImportRelation, ProjectionMetadata,
    ProjectionProvenance, Symbol, make_external_symbol_id, make_unresolved_callee_id,
};
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::{GraphClient, Row};

const CALL_TARGET_PREDICATE: &str =
    "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";
const NEIGHBOR_PREDICATE: &str =
    "neighbor:CodeSymbol OR neighbor:UnresolvedCallee OR neighbor:ExternalSymbol";
const PROJECT_NODE_PREDICATE: &str =
    "n:CodeFile OR n:CodeSymbol OR n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol";
const TARGET_TYPE_CASE: &str = "CASE \
     WHEN target:CodeSymbol THEN coalesce(target.kind, 'function') \
     WHEN target:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
const NEIGHBOR_TYPE_CASE: &str = "CASE \
     WHEN neighbor:CodeSymbol THEN coalesce(neighbor.kind, 'function') \
     WHEN neighbor:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
const NODE_TYPE_CASE: &str = "CASE \
     WHEN n:CodeFile THEN 'file' \
     WHEN n:CodeModule THEN 'module' \
     WHEN n:CodeSymbol THEN coalesce(n.kind, 'function') \
     WHEN n:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
const LINK_METADATA_RETURN: &str = "r.provenance AS provenance, \
     r.confidence AS confidence, \
     r.source_system AS source_system, \
     r.source_file_path AS source_file_path, \
     r.source_line AS source_line, \
     r.source_symbol_id AS source_symbol_id, \
     r.matching_method AS matching_method";
const MAX_GRAPH_LIMIT: usize = 100;
const EXTRACTED_PROVENANCE: &str = "EXTRACTED";
const SOURCE_SYSTEM_GCODE: &str = crate::models::SOURCE_SYSTEM_GCODE;

pub struct CodeGraph<'a> {
    project_id: &'a str,
    client: &'a mut GraphClient,
}

impl<'a> CodeGraph<'a> {
    pub fn new(project_id: &'a str, client: &'a mut GraphClient) -> Self {
        Self { project_id, client }
    }

    pub fn sync_file(
        &mut self,
        file_path: &str,
        imports: &[ImportRelation],
        definitions: &[Symbol],
        calls: &[CallRelation],
    ) -> anyhow::Result<usize> {
        self.ensure_file_node(file_path, definitions.len())?;
        let current_symbol_ids = definitions
            .iter()
            .map(|symbol| symbol.id.clone())
            .collect::<Vec<_>>();
        self.delete_file_graph(file_path, &current_symbol_ids)?;

        let mut relationship_count = 0;
        relationship_count += self.add_imports(file_path, imports)?;
        relationship_count += self.add_definitions(file_path, definitions)?;
        relationship_count += self.add_calls(file_path, calls)?;
        self.cleanup_orphans()?;
        Ok(relationship_count)
    }

    pub fn ensure_file_node(&mut self, file_path: &str, symbol_count: usize) -> anyhow::Result<()> {
        execute_write_query(
            self.client,
            ensure_file_node_query(self.project_id, file_path, symbol_count)?,
        )
    }

    pub fn add_imports(
        &mut self,
        file_path: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize> {
        let mut written = 0;
        for import in imports {
            if import.module_name.is_empty() {
                continue;
            }
            let source_file = if import.file_path.is_empty() {
                file_path
            } else {
                &import.file_path
            };
            execute_write_query(
                self.client,
                add_import_query(self.project_id, source_file, &import.module_name)?,
            )?;
            written += 1;
        }
        Ok(written)
    }

    pub fn add_definitions(
        &mut self,
        file_path: &str,
        definitions: &[Symbol],
    ) -> anyhow::Result<usize> {
        let mut written = 0;
        for symbol in definitions {
            if symbol.id.is_empty() || symbol.name.is_empty() {
                continue;
            }
            execute_write_query(
                self.client,
                add_definition_query(self.project_id, file_path, symbol)?,
            )?;
            written += 1;
        }
        Ok(written)
    }

    pub fn add_calls(&mut self, file_path: &str, calls: &[CallRelation]) -> anyhow::Result<usize> {
        let mut written = 0;
        for call in calls {
            if let Some(query) = add_call_query(self.project_id, file_path, call)? {
                execute_write_query(self.client, query)?;
                written += 1;
            }
        }
        Ok(written)
    }

    pub fn delete_file_graph(
        &mut self,
        file_path: &str,
        current_symbol_ids: &[String],
    ) -> anyhow::Result<()> {
        for query in delete_file_graph_queries(self.project_id, file_path, current_symbol_ids)? {
            execute_write_query(self.client, query)?;
        }
        Ok(())
    }

    pub fn cleanup_orphans(&mut self) -> anyhow::Result<()> {
        for query in cleanup_orphans_queries(self.project_id)? {
            execute_write_query(self.client, query)?;
        }
        Ok(())
    }

    pub fn clear_project(&mut self) -> anyhow::Result<()> {
        execute_write_query(self.client, clear_project_query(self.project_id)?)
    }
}

pub fn sync_file_graph(
    ctx: &Context,
    file_path: &str,
    imports: &[ImportRelation],
    definitions: &[Symbol],
    calls: &[CallRelation],
) -> anyhow::Result<usize> {
    with_core_graph(ctx, 0, |client| {
        CodeGraph::new(&ctx.project_id, client).sync_file(file_path, imports, definitions, calls)
    })
}

pub fn delete_file_graph(
    ctx: &Context,
    file_path: &str,
    current_symbol_ids: &[String],
) -> anyhow::Result<()> {
    with_core_graph(ctx, (), |client| {
        CodeGraph::new(&ctx.project_id, client).delete_file_graph(file_path, current_symbol_ids)
    })
}

pub fn cleanup_orphans(ctx: &Context) -> anyhow::Result<()> {
    with_core_graph(ctx, (), |client| {
        CodeGraph::new(&ctx.project_id, client).cleanup_orphans()
    })
}

pub fn clear_project(ctx: &Context) -> anyhow::Result<()> {
    with_core_graph(ctx, (), |client| {
        CodeGraph::new(&ctx.project_id, client).clear_project()
    })
}

fn execute_write_query(client: &mut GraphClient, query: TypedQuery) -> anyhow::Result<()> {
    let TypedQuery { cypher, params } = query;
    client.query(&cypher, Some(params))?;
    Ok(())
}

fn typed_query<I, K>(cypher: impl Into<String>, params: I) -> anyhow::Result<TypedQuery>
where
    I: IntoIterator<Item = (K, TypedValue)>,
    K: Into<String>,
{
    Ok(TypedQuery::with_params(cypher, params)?)
}

fn usize_value(value: usize) -> TypedValue {
    TypedValue::Integer(value.min(i64::MAX as usize) as i64)
}

fn optional_string_value(value: Option<&str>) -> TypedValue {
    value
        .filter(|value| !value.is_empty())
        .map(|value| TypedValue::String(value.to_string()))
        .unwrap_or(TypedValue::Null)
}

fn base_metadata_params(file_path: &str) -> Vec<(&'static str, TypedValue)> {
    vec![
        (
            "provenance",
            TypedValue::String(EXTRACTED_PROVENANCE.to_string()),
        ),
        ("confidence", TypedValue::Float(1.0)),
        (
            "source_system",
            TypedValue::String(SOURCE_SYSTEM_GCODE.to_string()),
        ),
        (
            "source_file_path",
            TypedValue::String(file_path.to_string()),
        ),
    ]
}

fn extracted_edge_params(
    file_path: &str,
    source_line: usize,
    source_symbol_id: Option<&str>,
) -> Vec<(&'static str, TypedValue)> {
    let mut params = base_metadata_params(file_path);
    params.push(("source_line", usize_value(source_line)));
    params.push(("source_symbol_id", optional_string_value(source_symbol_id)));
    params
}

pub(crate) fn ensure_file_node_query(
    project_id: &str,
    file_path: &str,
    symbol_count: usize,
) -> anyhow::Result<TypedQuery> {
    typed_query(
        "MERGE (f:CodeFile {path: $file_path, project: $project})
         SET f.updated_at = timestamp(), f.symbol_count = $symbol_count",
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
            ("symbol_count", usize_value(symbol_count)),
        ],
    )
}

pub(crate) fn add_import_query(
    project_id: &str,
    source_file: &str,
    target_module: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("source_file", TypedValue::String(source_file.to_string())),
        (
            "target_module",
            TypedValue::String(target_module.to_string()),
        ),
    ];
    params.extend(base_metadata_params(source_file));
    typed_query(
        "MERGE (f:CodeFile {path: $source_file, project: $project})
         MERGE (m:CodeModule {name: $target_module, project: $project})
         MERGE (f)-[r:IMPORTS]->(m)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = $source_file_path",
        params,
    )
}

pub(crate) fn add_definition_query(
    project_id: &str,
    file_path: &str,
    symbol: &Symbol,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("file_path", TypedValue::String(file_path.to_string())),
        ("symbol_id", TypedValue::String(symbol.id.clone())),
        ("name", TypedValue::String(symbol.name.clone())),
        (
            "qualified_name",
            TypedValue::String(symbol.qualified_name.clone()),
        ),
        ("kind", TypedValue::String(symbol.kind.clone())),
        ("language", TypedValue::String(symbol.language.clone())),
        ("line_start", usize_value(symbol.line_start)),
        ("line_end", usize_value(symbol.line_end)),
    ];
    params.extend(extracted_edge_params(
        file_path,
        symbol.line_start,
        Some(&symbol.id),
    ));
    typed_query(
        "MERGE (f:CodeFile {path: $file_path, project: $project})
         MERGE (s:CodeSymbol {id: $symbol_id, project: $project})
         SET s.name = $name,
             s.qualified_name = $qualified_name,
             s.kind = $kind,
             s.language = $language,
             s.file_path = $file_path,
             s.line_start = $line_start,
             s.line_end = $line_end,
             s.updated_at = timestamp()
         MERGE (f)-[r:DEFINES]->(s)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = $source_file_path,
             r.source_line = $source_line,
             r.source_symbol_id = $source_symbol_id",
        params,
    )
}

enum GraphCallTarget {
    Symbol { id: String },
    External { id: String, module: String },
    Unresolved { id: String },
}

impl GraphCallTarget {
    fn from_call(project_id: &str, call: &CallRelation) -> Option<Self> {
        if let Some(id) = call.callee_symbol_id.as_deref().filter(|id| !id.is_empty()) {
            return Some(Self::Symbol { id: id.to_string() });
        }
        if call.callee_name.is_empty() {
            return None;
        }
        if call.callee_target_kind == CallTargetKind::External {
            let module = call.callee_external_module.clone().unwrap_or_default();
            return Some(Self::External {
                id: make_external_symbol_id(project_id, &call.callee_name, Some(&module)),
                module,
            });
        }
        Some(Self::Unresolved {
            id: make_unresolved_callee_id(project_id, &call.callee_name),
        })
    }
}

pub fn call_target_id(project_id: &str, call: &CallRelation) -> Option<String> {
    match GraphCallTarget::from_call(project_id, call)? {
        GraphCallTarget::Symbol { id }
        | GraphCallTarget::External { id, .. }
        | GraphCallTarget::Unresolved { id } => Some(id),
    }
}

pub(crate) fn add_call_query(
    project_id: &str,
    default_file_path: &str,
    call: &CallRelation,
) -> anyhow::Result<Option<TypedQuery>> {
    if call.caller_symbol_id.is_empty() {
        return Ok(None);
    }
    let Some(target) = GraphCallTarget::from_call(project_id, call) else {
        return Ok(None);
    };
    let file_path = if call.file_path.is_empty() {
        default_file_path
    } else {
        &call.file_path
    };
    let target_id = match &target {
        GraphCallTarget::Symbol { id }
        | GraphCallTarget::External { id, .. }
        | GraphCallTarget::Unresolved { id } => id,
    };
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        (
            "caller_id",
            TypedValue::String(call.caller_symbol_id.clone()),
        ),
        ("target_id", TypedValue::String(target_id.clone())),
        ("callee_name", TypedValue::String(call.callee_name.clone())),
        ("file_path", TypedValue::String(file_path.to_string())),
        ("line", usize_value(call.line)),
    ];
    params.extend(extracted_edge_params(
        file_path,
        call.line,
        Some(&call.caller_symbol_id),
    ));

    let cypher = match target {
        GraphCallTarget::Symbol { .. } => {
            "MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
             MERGE (callee:CodeSymbol {id: $target_id, project: $project})
             ON CREATE SET callee.name = $callee_name, callee.updated_at = timestamp()
             MERGE (caller)-[r:CALLS {file: $file_path, line: $line}]->(callee)
             SET r.provenance = $provenance,
                 r.confidence = $confidence,
                 r.source_system = $source_system,
                 r.source_file_path = $source_file_path,
                 r.source_line = $source_line,
                 r.source_symbol_id = $source_symbol_id"
                .to_string()
        }
        GraphCallTarget::External { module, .. } => {
            params.push(("callee_module", TypedValue::String(module)));
            "MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
             MERGE (callee:ExternalSymbol {id: $target_id, project: $project})
             SET callee.name = $callee_name,
                 callee.external_module = $callee_module,
                 callee.module = $callee_module,
                 callee.updated_at = timestamp()
             MERGE (caller)-[r:CALLS {file: $file_path, line: $line}]->(callee)
             SET r.provenance = $provenance,
                 r.confidence = $confidence,
                 r.source_system = $source_system,
                 r.source_file_path = $source_file_path,
                 r.source_line = $source_line,
                 r.source_symbol_id = $source_symbol_id"
                .to_string()
        }
        GraphCallTarget::Unresolved { .. } => {
            "MERGE (caller:CodeSymbol {id: $caller_id, project: $project})
             MERGE (callee:UnresolvedCallee {id: $target_id, project: $project})
             SET callee.name = $callee_name,
                 callee.updated_at = timestamp()
             MERGE (caller)-[r:CALLS {file: $file_path, line: $line}]->(callee)
             SET r.provenance = $provenance,
                 r.confidence = $confidence,
                 r.source_system = $source_system,
                 r.source_file_path = $source_file_path,
                 r.source_line = $source_line,
                 r.source_symbol_id = $source_symbol_id"
                .to_string()
        }
    };

    Ok(Some(typed_query(cypher, params)?))
}

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

pub(crate) fn cleanup_orphans_queries(project_id: &str) -> anyhow::Result<Vec<TypedQuery>> {
    let project_param = || [("project", TypedValue::String(project_id.to_string()))];
    Ok(vec![
        typed_query(
            "MATCH (m:CodeModule {project: $project})
             WHERE NOT (m)<-[:IMPORTS]-()
             DETACH DELETE m",
            project_param(),
        )?,
        typed_query(
            "MATCH (n {project: $project})
             WHERE (n:UnresolvedCallee OR n:ExternalSymbol)
               AND NOT ()-[:CALLS]->(n)
             DETACH DELETE n",
            project_param(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project})
             WHERE s.file_path IS NULL
               AND NOT ()-[:DEFINES]->(s)
               AND NOT ()-[:CALLS]->(s)
               AND NOT (s)-[:CALLS]->()
             DETACH DELETE s",
            project_param(),
        )?,
    ])
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphLifecycleAction {
    Clear,
    Rebuild,
}

impl GraphLifecycleAction {
    pub fn cli_command(self) -> &'static str {
        match self {
            Self::Clear => "gcode graph clear",
            Self::Rebuild => "gcode graph rebuild",
        }
    }

    pub fn endpoint_path(self) -> &'static str {
        match self {
            Self::Clear => "/api/code-index/graph/clear",
            Self::Rebuild => "/api/code-index/graph/rebuild",
        }
    }

    pub fn success_prefix(self) -> &'static str {
        match self {
            Self::Clear => "Cleared code-index graph",
            Self::Rebuild => "Rebuilt code-index graph",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphLifecycleRequest {
    pub project_id: String,
    pub daemon_url: Option<String>,
}

impl GraphLifecycleRequest {
    pub fn from_context(ctx: &Context) -> Self {
        Self {
            project_id: ctx.project_id.clone(),
            daemon_url: ctx.daemon_url.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GraphLifecycleOutput {
    pub project_id: String,
    pub action: GraphLifecycleAction,
    pub summary: String,
    pub payload: Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphReadRequest {
    pub project_id: String,
    pub symbol_id: String,
    pub offset: usize,
    pub limit: usize,
    pub depth: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct GraphPayload {
    pub nodes: Vec<GraphNode>,
    pub links: Vec<GraphLink>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub center: Option<String>,
}

impl GraphPayload {
    pub fn with_center(center: impl Into<String>) -> Self {
        Self {
            nodes: vec![],
            links: vec![],
            center: Some(center.into()),
        }
    }

    pub fn push_node(&mut self, node: GraphNode) {
        if node.id.is_empty() || self.nodes.iter().any(|existing| existing.id == node.id) {
            return;
        }
        self.nodes.push(node);
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GraphNode {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub node_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub symbol_count: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blast_distance: Option<usize>,
}

impl GraphNode {
    pub fn new(
        id: impl Into<String>,
        name: impl Into<String>,
        node_type: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            node_type: node_type.into(),
            kind: None,
            file_path: None,
            line_start: None,
            signature: None,
            symbol_count: None,
            language: None,
            blast_distance: None,
        }
    }

    fn from_row(row: &Row, default_type: &str) -> Option<Self> {
        let id = row_string(row, &["id", "node_id"])?;
        let mut node = Self::new(
            id.clone(),
            row_string(row, &["name", "node_name"]).unwrap_or(id),
            row_string(row, &["type", "node_type"]).unwrap_or_else(|| default_type.to_string()),
        );
        node.kind = row_string(row, &["kind"]);
        node.file_path = row_string(row, &["file_path"]);
        node.line_start = row_usize(row, &["line_start", "line"]);
        node.signature = row_string(row, &["signature"]);
        node.symbol_count = row_usize(row, &["symbol_count"]);
        node.language = row_string(row, &["language"]);
        node.blast_distance = row_usize(row, &["blast_distance", "distance"]);
        Some(node)
    }

    fn from_prefixed_row(row: &Row, prefix: &str, default_type: &str) -> Option<Self> {
        let id_key = format!("{prefix}_id");
        let name_key = format!("{prefix}_name");
        let type_key = format!("{prefix}_type");
        let kind_key = format!("{prefix}_kind");
        let file_path_key = format!("{prefix}_file_path");
        let line_start_key = format!("{prefix}_line_start");
        let signature_key = format!("{prefix}_signature");

        let id = row_string_owned(row, &[id_key.as_str()])?;
        let mut node = Self::new(
            id.clone(),
            row_string_owned(row, &[name_key.as_str()]).unwrap_or(id),
            row_string_owned(row, &[type_key.as_str()]).unwrap_or_else(|| default_type.to_string()),
        );
        node.kind = row_string_owned(row, &[kind_key.as_str()]);
        node.file_path = row_string_owned(row, &[file_path_key.as_str()]);
        node.line_start = row_usize_owned(row, &[line_start_key.as_str()]);
        node.signature = row_string_owned(row, &[signature_key.as_str()]);
        Some(node)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GraphLink {
    pub source: String,
    pub target: String,
    #[serde(rename = "type")]
    pub link_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub distance: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<ProjectionMetadata>,
}

impl GraphLink {
    pub fn new(
        source: impl Into<String>,
        target: impl Into<String>,
        link_type: impl Into<String>,
    ) -> Self {
        Self {
            source: source.into(),
            target: target.into(),
            link_type: link_type.into(),
            line: None,
            distance: None,
            metadata: None,
        }
    }

    pub fn from_row(row: &Row) -> Self {
        let mut link = Self::new(
            row_string(row, &["source"]).unwrap_or_default(),
            row_string(row, &["target"]).unwrap_or_default(),
            row_string(row, &["type", "rel_type"]).unwrap_or_else(|| "CALLS".to_string()),
        );
        link.line = row_usize(row, &["line"]);
        link.distance = row_usize(row, &["distance"]);
        link.metadata = row_to_projection_metadata(row);
        link
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphBlastRadiusTarget {
    SymbolId(String),
    FilePath(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphReadError {
    NotConfigured,
    Unreachable { message: String },
    QueryFailed { message: String },
    InvalidTarget { message: String },
}

impl fmt::Display for GraphReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotConfigured => {
                f.write_str("FalkorDB is not configured; graph read APIs require FalkorDB")
            }
            Self::Unreachable { message } => {
                write!(
                    f,
                    "FalkorDB is unreachable; graph read APIs require FalkorDB: {message}"
                )
            }
            Self::QueryFailed { message } => {
                write!(f, "FalkorDB graph read failed: {message}")
            }
            Self::InvalidTarget { message } => f.write_str(message),
        }
    }
}

impl std::error::Error for GraphReadError {}

pub fn require_daemon_url(
    daemon_url: Option<&str>,
    action: GraphLifecycleAction,
) -> anyhow::Result<&str> {
    daemon_url.ok_or_else(|| {
        anyhow::anyhow!(
            "Gobby daemon URL is not configured. `{}` requires the Gobby daemon.",
            action.cli_command()
        )
    })
}

pub(crate) fn build_lifecycle_url(
    base_url: &str,
    action: GraphLifecycleAction,
    project_id: &str,
) -> anyhow::Result<reqwest::Url> {
    let base = base_url.trim_end_matches('/');
    let mut url = reqwest::Url::parse(&format!("{base}{}", action.endpoint_path()))
        .with_context(|| format!("invalid Gobby daemon URL: {base_url}"))?;
    url.query_pairs_mut().append_pair("project_id", project_id);
    Ok(url)
}

fn compact_detail(body: &str) -> String {
    let detail = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let detail = detail.trim();
    if detail.len() > 240 {
        format!("{}...", &detail[..237])
    } else {
        detail.to_string()
    }
}

pub(crate) fn format_http_error(
    action: GraphLifecycleAction,
    url: &reqwest::Url,
    status: StatusCode,
    body: &str,
) -> String {
    let detail = compact_detail(body);
    if detail.is_empty() {
        format!(
            "`{}` failed: daemon returned HTTP {status} from {url}",
            action.cli_command()
        )
    } else {
        format!(
            "`{}` failed: daemon returned HTTP {status} from {url}: {detail}",
            action.cli_command()
        )
    }
}

pub(crate) fn parse_success_payload(
    action: GraphLifecycleAction,
    status: StatusCode,
    body: &str,
) -> anyhow::Result<Value> {
    serde_json::from_str(body).map_err(|err| {
        let detail = compact_detail(body);
        if detail.is_empty() {
            anyhow::anyhow!(
                "`{}` failed: daemon returned HTTP {status} with invalid JSON: {err}",
                action.cli_command()
            )
        } else {
            anyhow::anyhow!(
                "`{}` failed: daemon returned HTTP {status} with invalid JSON: {err}. Response: {detail}",
                action.cli_command()
            )
        }
    })
}

fn extract_summary_text(payload: &Value) -> Option<String> {
    match payload {
        Value::String(text) => {
            let text = text.trim();
            (!text.is_empty()).then(|| text.to_string())
        }
        Value::Object(map) => ["summary", "message", "detail", "status"]
            .iter()
            .find_map(|key| map.get(*key).and_then(Value::as_str))
            .map(str::trim)
            .filter(|text| !text.is_empty())
            .map(ToOwned::to_owned),
        _ => None,
    }
}

pub fn run_lifecycle_action(
    request: &GraphLifecycleRequest,
    action: GraphLifecycleAction,
) -> anyhow::Result<GraphLifecycleOutput> {
    let daemon_url = require_daemon_url(request.daemon_url.as_deref(), action)?;
    let url = build_lifecycle_url(daemon_url, action, &request.project_id)?;
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .context("failed to build HTTP client")?;

    let response = client
        .post(url.clone())
        .header("Accept", "application/json")
        .send()
        .with_context(|| {
            format!(
                "Failed to reach Gobby daemon at {daemon_url} for `{}`",
                action.cli_command()
            )
        })?;

    let status = response.status();
    let body = response.text().unwrap_or_default();
    if !status.is_success() {
        anyhow::bail!("{}", format_http_error(action, &url, status, &body));
    }

    let payload = parse_success_payload(action, status, &body)?;
    let summary = extract_summary_text(&payload).unwrap_or_else(|| payload.to_string());
    Ok(GraphLifecycleOutput {
        project_id: request.project_id.clone(),
        action,
        summary,
        payload,
    })
}

pub(crate) fn row_to_graph_result(row: &Row) -> GraphResult {
    GraphResult {
        id: row
            .get("caller_id")
            .or_else(|| row.get("callee_id"))
            .or_else(|| row.get("source_id"))
            .or_else(|| row.get("node_id"))
            .or_else(|| row.get("symbol_id"))
            .or_else(|| row.get("id"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        name: row
            .get("caller_name")
            .or_else(|| row.get("callee_name"))
            .or_else(|| row.get("source_name"))
            .or_else(|| row.get("node_name"))
            .or_else(|| row.get("symbol_name"))
            .or_else(|| row.get("name"))
            .or_else(|| row.get("module_name"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        file_path: row
            .get("file")
            .or_else(|| row.get("file_path"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        line: row.get("line").and_then(|v| v.as_u64()).unwrap_or(0) as usize,
        relation: row
            .get("relation")
            .or_else(|| row.get("rel_type"))
            .and_then(|v| v.as_str())
            .map(String::from),
        distance: row
            .get("distance")
            .and_then(|v| v.as_u64())
            .map(|d| d as usize),
        metadata: row_to_projection_metadata(row),
    }
}

pub fn extracted_code_edge_metadata(
    file_path: impl Into<String>,
    line: usize,
    source_symbol_id: Option<&str>,
) -> ProjectionMetadata {
    let mut metadata = ProjectionMetadata::gcode_extracted()
        .with_source_file_path(file_path)
        .with_source_line(line);
    if let Some(source_symbol_id) = source_symbol_id {
        metadata = metadata.with_source_symbol_id(source_symbol_id);
    }
    metadata
}

fn row_to_projection_metadata(row: &Row) -> Option<ProjectionMetadata> {
    let provenance = row
        .get("provenance")
        .and_then(|v| v.as_str())
        .and_then(ProjectionProvenance::from_wire_value)?;
    let source_system = row.get("source_system").and_then(|v| v.as_str())?;

    let mut metadata = ProjectionMetadata::new(provenance, source_system);
    metadata.confidence = row.get("confidence").and_then(|v| v.as_f64());
    metadata.source_file_path = row
        .get("source_file_path")
        .or_else(|| row.get("file"))
        .or_else(|| row.get("file_path"))
        .and_then(|v| v.as_str())
        .map(ToOwned::to_owned);
    metadata.source_line = row
        .get("source_line")
        .or_else(|| row.get("line"))
        .and_then(|v| v.as_u64())
        .map(|line| line as usize);
    metadata.source_symbol_id = row
        .get("source_symbol_id")
        .or_else(|| row.get("caller_id"))
        .or_else(|| row.get("source_id"))
        .and_then(|v| v.as_str())
        .map(ToOwned::to_owned);
    metadata.matching_method = row
        .get("matching_method")
        .and_then(|v| v.as_str())
        .map(ToOwned::to_owned);
    Some(metadata)
}

fn row_string(row: &Row, keys: &[&str]) -> Option<String> {
    row_string_owned(row, keys)
}

fn row_string_owned(row: &Row, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| row.get(*key).and_then(|value| value.as_str()))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn row_usize(row: &Row, keys: &[&str]) -> Option<usize> {
    row_usize_owned(row, keys)
}

fn row_usize_owned(row: &Row, keys: &[&str]) -> Option<usize> {
    keys.iter()
        .find_map(|key| row.get(*key))
        .and_then(|value| {
            value
                .as_u64()
                .or_else(|| value.as_i64().and_then(|value| value.try_into().ok()))
        })
        .map(|value| value as usize)
}

fn add_link_from_row(payload: &mut GraphPayload, row: &Row) {
    let link = GraphLink::from_row(row);
    if link.source.is_empty() || link.target.is_empty() {
        return;
    }
    payload.links.push(link);
}

fn add_node_from_row(payload: &mut GraphPayload, row: &Row, default_type: &str) {
    if let Some(node) = GraphNode::from_row(row, default_type) {
        payload.push_node(node);
    }
}

fn add_prefixed_node_from_row(
    payload: &mut GraphPayload,
    row: &Row,
    prefix: &str,
    default_type: &str,
) {
    if let Some(node) = GraphNode::from_prefixed_row(row, prefix, default_type) {
        payload.push_node(node);
    }
}

fn clamp_limit(limit: usize) -> usize {
    typed_query::clamp_limit(limit, MAX_GRAPH_LIMIT)
}

fn clamp_offset(offset: usize) -> usize {
    typed_query::clamp_offset(offset, MAX_GRAPH_LIMIT)
}

pub(crate) fn count_callers_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(caller) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn count_usages_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(source) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_callers_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    r.file AS file, r.line AS line \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_usages_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN source.id AS source_id, source.name AS source_name, \
                    'CALLS' AS rel_type, r.file AS file, r.line AS line \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_callers_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) AND target.id IN [{ids}] \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    r.file AS file, r.line AS line \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn find_callees_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (src:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE src.id IN [{ids}] AND ({CALL_TARGET_PREDICATE}) \
             RETURN target.id AS callee_id, target.name AS callee_name, \
                    r.file AS file, r.line AS line \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn get_imports_query(
    project_id: &str,
    file_path: &str,
) -> (String, HashMap<String, String>) {
    (
        "MATCH (f:CodeFile {path: $path, project: $project})-[:IMPORTS]->(m:CodeModule) \
         RETURN m.name AS module_name"
            .to_string(),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub(crate) fn blast_radius_query(depth: usize, limit: usize) -> String {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
    format!(
        "MATCH (target {{id: $id, project: $project}}) \
         WHERE {CALL_TARGET_PREDICATE} \
         MATCH path = (affected:CodeSymbol {{project: $project}})-[:CALLS*1..{depth}]->(target) \
         WITH affected, min(length(path)) AS distance \
         OPTIONAL MATCH (file:CodeFile {{project: $project}})-[:DEFINES]->(affected) \
         RETURN DISTINCT affected.id AS node_id, \
                affected.name AS node_name, \
                affected.kind AS kind, file.path AS file_path, \
                affected.line_start AS line, \
                distance, 'call' AS rel_type \
         ORDER BY distance ASC, affected.name ASC \
         LIMIT {limit}"
    )
}

fn project_overview_files_query(
    project_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}}) \
             OPTIONAL MATCH (f)-[:DEFINES]->(s:CodeSymbol) \
             WITH f, count(DISTINCT s) AS sym_count \
             OPTIONAL MATCH (f)-[:IMPORTS]->(m:CodeModule) \
             WITH f, sym_count, count(m) AS imp_count \
             RETURN f.path AS id, f.path AS name, 'file' AS type, \
                    f.path AS file_path, sym_count AS symbol_count \
             ORDER BY imp_count DESC, sym_count DESC, f.path \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn project_overview_imports_query(
    project_id: &str,
    file_paths: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let file_paths = typed_query::id_list_literal(file_paths);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}})-[r:IMPORTS]->(m:CodeModule {{project: $project}}) \
             WHERE f.path IN [{file_paths}] \
             RETURN f.path AS source, m.name AS target, 'IMPORTS' AS type, {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn project_overview_defines_query(
    project_id: &str,
    file_paths: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let file_paths = typed_query::id_list_literal(file_paths);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}})-[r:DEFINES]->(s:CodeSymbol {{project: $project}}) \
             WHERE f.path IN [{file_paths}] \
             RETURN f.path AS source, s.id AS target, 'DEFINES' AS type, \
                    s.name AS symbol_name, s.kind AS symbol_kind, \
                    s.file_path AS symbol_file_path, s.line_start AS line_start, \
                    {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn project_overview_calls_query(
    project_id: &str,
    file_paths: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let file_paths = typed_query::id_list_literal(file_paths);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}})-[:DEFINES]->(s:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE f.path IN [{file_paths}] AND ({CALL_TARGET_PREDICATE}) \
             RETURN s.id AS source, target.id AS target, 'CALLS' AS type, \
                    target.name AS target_name, {TARGET_TYPE_CASE} AS target_type, \
                    target.kind AS target_kind, target.file_path AS target_file_path, \
                    target.line_start AS target_line_start, r.line AS line, \
                    {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn file_symbols_query(project_id: &str, file_path: &str) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (:CodeFile {{path: $path, project: $project}})-[r:DEFINES]->(s:CodeSymbol {{project: $project}}) \
             RETURN s.id AS id, s.name AS name, coalesce(s.kind, 'function') AS type, \
                    s.kind AS kind, s.file_path AS file_path, \
                    s.line_start AS line_start, s.signature AS signature, \
                    {LINK_METADATA_RETURN}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

fn file_calls_query(project_id: &str, file_path: &str) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) \
               AND (source.file_path = $path OR (target:CodeSymbol AND target.file_path = $path)) \
             RETURN source.id AS source_id, source.name AS source_name, \
                    coalesce(source.kind, 'function') AS source_type, \
                    source.kind AS source_kind, source.file_path AS source_file_path, \
                    source.line_start AS source_line_start, source.signature AS source_signature, \
                    target.id AS target_id, target.name AS target_name, \
                    {TARGET_TYPE_CASE} AS target_type, target.kind AS target_kind, \
                    target.file_path AS target_file_path, \
                    target.line_start AS target_line_start, target.signature AS target_signature, \
                    source.id AS source, target.id AS target, 'CALLS' AS type, r.line AS line, \
                    {LINK_METADATA_RETURN}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

fn symbol_neighbors_query(
    project_id: &str,
    symbol_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (center {{id: $id, project: $project}}) \
             WHERE center:CodeSymbol OR center:UnresolvedCallee OR center:ExternalSymbol \
             MATCH (center)-[r:CALLS]-(neighbor {{project: $project}}) \
             WHERE {NEIGHBOR_PREDICATE} \
             RETURN neighbor.id AS id, neighbor.name AS name, {NEIGHBOR_TYPE_CASE} AS type, \
                    neighbor.kind AS kind, neighbor.file_path AS file_path, \
                    neighbor.line_start AS line_start, neighbor.signature AS signature, \
                    CASE WHEN startNode(r) = center THEN 'outgoing' ELSE 'incoming' END AS direction, \
                    r.line AS line, {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

fn blast_radius_center_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (n {{id: $id, project: $project}}) \
             WHERE n:CodeSymbol OR n:UnresolvedCallee OR n:ExternalSymbol \
             RETURN n.id AS id, n.name AS name, {NODE_TYPE_CASE} AS type, \
                    n.kind AS kind, n.file_path AS file_path \
             LIMIT 1"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

fn blast_radius_file_call_query(
    project_id: &str,
    file_path: &str,
    depth: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (tf:CodeFile {{path: $path, project: $project}})-[:DEFINES]->(target_sym:CodeSymbol {{project: $project}}) \
             MATCH path = (affected:CodeSymbol {{project: $project}})-[:CALLS*1..{depth}]->(target_sym) \
             WITH affected, min(length(path)) AS distance \
             OPTIONAL MATCH (file:CodeFile {{project: $project}})-[:DEFINES]->(affected) \
             RETURN DISTINCT affected.id AS node_id, \
                    affected.name AS node_name, \
                    affected.kind AS kind, file.path AS file_path, \
                    affected.line_start AS line, distance, 'call' AS rel_type, \
                    coalesce(affected.kind, 'function') AS node_type \
             ORDER BY distance ASC, affected.name ASC \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

fn blast_radius_file_import_query(
    project_id: &str,
    file_path: &str,
    depth: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (tf:CodeFile {{path: $path, project: $project}})-[:IMPORTS]->(m:CodeModule {{project: $project}}) \
             MATCH path = (importer:CodeFile {{project: $project}})-[:IMPORTS*1..{depth}]->(m) \
             WHERE importer.path <> $path \
             WITH importer, min(length(path)) AS distance \
             RETURN DISTINCT importer.path AS node_id, \
                    importer.path AS node_name, NULL AS kind, importer.path AS file_path, \
                    NULL AS line, distance, 'import' AS rel_type, 'file' AS node_type \
             ORDER BY distance ASC \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

fn count_from_rows(rows: &[Row]) -> usize {
    rows.first()
        .and_then(|r| r.get("cnt"))
        .and_then(|v| {
            v.as_u64()
                .or_else(|| v.as_i64().and_then(|value| value.try_into().ok()))
        })
        .unwrap_or(0) as usize
}

pub fn require_graph_reads(ctx: &Context) -> anyhow::Result<()> {
    if ctx.falkordb.is_none() {
        return Err(GraphReadError::NotConfigured.into());
    }
    Ok(())
}

fn with_required_core_graph<T>(
    ctx: &Context,
    f: impl FnOnce(&mut GraphClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let config = ctx.falkordb.as_ref().ok_or(GraphReadError::NotConfigured)?;
    let connection_config = config.connection_config();
    match gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        None,
        |client| f(client).map(Some),
    ) {
        Ok((Some(value), ServiceState::Available)) => Ok(value),
        Ok((_, ServiceState::NotConfigured)) => Err(GraphReadError::NotConfigured.into()),
        Ok((_, ServiceState::Unreachable { message })) => {
            Err(GraphReadError::Unreachable { message }.into())
        }
        Ok((None, ServiceState::Available)) => Err(GraphReadError::QueryFailed {
            message: "graph read returned no value".to_string(),
        }
        .into()),
        Err(error) => Err(GraphReadError::QueryFailed {
            message: error.to_string(),
        }
        .into()),
    }
}

pub fn project_overview_graph(ctx: &Context, limit: usize) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let limit = clamp_limit(limit);
        let link_limit = clamp_limit(limit.saturating_mul(4));
        let max_nodes = limit.saturating_mul(8);

        let (query, params) = project_overview_files_query(&ctx.project_id, limit);
        let file_rows = client.query(&query, Some(params))?;
        let mut payload = GraphPayload::default();
        for row in &file_rows {
            add_node_from_row(&mut payload, row, "file");
        }

        let file_paths = payload
            .nodes
            .iter()
            .filter(|node| node.node_type == "file")
            .map(|node| node.id.clone())
            .collect::<Vec<_>>();
        if file_paths.is_empty() {
            return Ok(payload);
        }

        let (query, params) =
            project_overview_imports_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(module_id) = row_string(&row, &["target"]) {
                payload.push_node(GraphNode::new(module_id.clone(), module_id, "module"));
            }
            if payload.nodes.len() >= max_nodes {
                break;
            }
        }

        let (query, params) =
            project_overview_defines_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(symbol_id) = row_string(&row, &["target"]) {
                let mut node = GraphNode::new(
                    symbol_id.clone(),
                    row_string(&row, &["symbol_name"]).unwrap_or(symbol_id),
                    row_string(&row, &["symbol_kind"]).unwrap_or_else(|| "function".to_string()),
                );
                node.kind = row_string(&row, &["symbol_kind"]);
                node.file_path = row_string(&row, &["symbol_file_path", "source"]);
                node.line_start = row_usize(&row, &["line_start"]);
                payload.push_node(node);
            }
            if payload.nodes.len() >= max_nodes {
                break;
            }
        }

        let (query, params) =
            project_overview_calls_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(target_id) = row_string(&row, &["target"]) {
                let mut node = GraphNode::new(
                    target_id.clone(),
                    row_string(&row, &["target_name"]).unwrap_or(target_id),
                    row_string(&row, &["target_type"]).unwrap_or_else(|| "unresolved".to_string()),
                );
                node.kind = row_string(&row, &["target_kind"]);
                node.file_path = row_string(&row, &["target_file_path"]);
                node.line_start = row_usize(&row, &["target_line_start"]);
                payload.push_node(node);
            }
            if payload.nodes.len() >= max_nodes {
                break;
            }
        }

        Ok(payload)
    })
}

pub fn file_graph(ctx: &Context, file_path: &str) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let mut payload = GraphPayload::default();
        let (query, params) = file_symbols_query(&ctx.project_id, file_path);
        for row in client.query(&query, Some(params))? {
            add_node_from_row(&mut payload, &row, "function");
            if let Some(symbol_id) = row_string(&row, &["id"]) {
                let mut link = GraphLink::new(file_path, symbol_id, "DEFINES");
                link.metadata = row_to_projection_metadata(&row);
                payload.links.push(link);
            }
        }

        let (query, params) = file_calls_query(&ctx.project_id, file_path);
        for row in client.query(&query, Some(params))? {
            add_prefixed_node_from_row(&mut payload, &row, "source", "function");
            add_prefixed_node_from_row(&mut payload, &row, "target", "unresolved");
            add_link_from_row(&mut payload, &row);
        }

        Ok(payload)
    })
}

pub fn symbol_neighbors(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = symbol_neighbors_query(&ctx.project_id, symbol_id, limit);
        let rows = client.query(&query, Some(params))?;
        let mut payload = GraphPayload::default();

        for row in rows {
            add_node_from_row(&mut payload, &row, "unresolved");
            let Some(neighbor_id) = row_string(&row, &["id"]) else {
                continue;
            };
            let direction = row_string(&row, &["direction"]).unwrap_or_default();
            let mut link = if direction == "outgoing" {
                GraphLink::new(symbol_id, neighbor_id, "CALLS")
            } else {
                GraphLink::new(neighbor_id, symbol_id, "CALLS")
            };
            link.line = row_usize(&row, &["line"]);
            link.metadata = row_to_projection_metadata(&row);
            payload.links.push(link);
        }

        Ok(payload)
    })
}

pub fn blast_radius_graph(
    ctx: &Context,
    target: GraphBlastRadiusTarget,
    depth: usize,
    limit: usize,
) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let (center_id, mut center_node, rows) = match target {
            GraphBlastRadiusTarget::SymbolId(symbol_id) => {
                let (query, params) = blast_radius_center_query(&ctx.project_id, &symbol_id);
                let center_rows = client.query(&query, Some(params))?;
                let center_node = center_rows
                    .first()
                    .and_then(|row| GraphNode::from_row(row, "function"))
                    .unwrap_or_else(|| GraphNode::new(&symbol_id, &symbol_id, "function"));

                let query = blast_radius_query(depth, limit);
                let params =
                    typed_query::string_params(&[("project", &ctx.project_id), ("id", &symbol_id)]);
                (symbol_id, center_node, client.query(&query, Some(params))?)
            }
            GraphBlastRadiusTarget::FilePath(file_path) => {
                let mut rows = vec![];
                let (query, params) =
                    blast_radius_file_call_query(&ctx.project_id, &file_path, depth, limit);
                rows.extend(client.query(&query, Some(params))?);
                let (query, params) =
                    blast_radius_file_import_query(&ctx.project_id, &file_path, depth, limit);
                rows.extend(client.query(&query, Some(params))?);
                (
                    file_path.clone(),
                    GraphNode::new(&file_path, &file_path, "file"),
                    rows,
                )
            }
        };

        center_node.blast_distance = Some(0);
        let mut payload = GraphPayload::with_center(center_id.clone());
        payload.push_node(center_node);

        for row in rows {
            let Some(node_id) = row_string(&row, &["node_id"]) else {
                continue;
            };
            let mut node = GraphNode::new(
                node_id.clone(),
                row_string(&row, &["node_name"]).unwrap_or_else(|| node_id.clone()),
                row_string(&row, &["node_type"]).unwrap_or_else(|| "function".to_string()),
            );
            node.kind = row_string(&row, &["kind"]);
            node.file_path = row_string(&row, &["file_path"]);
            node.line_start = row_usize(&row, &["line"]);
            node.blast_distance = row_usize(&row, &["distance"]);
            payload.push_node(node);

            let relation = row_string(&row, &["rel_type"]).unwrap_or_else(|| "call".to_string());
            let mut link = GraphLink::new(
                node_id,
                &center_id,
                if relation == "call" {
                    "CALLS"
                } else {
                    "IMPORTS"
                },
            );
            link.distance = row_usize(&row, &["distance"]);
            link.metadata = row_to_projection_metadata(&row);
            payload.links.push(link);
        }

        Ok(payload)
    })
}

fn with_core_graph<T: Clone>(
    ctx: &Context,
    default: T,
    f: impl FnOnce(&mut GraphClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let Some(config) = ctx.falkordb.as_ref() else {
        return Ok(default);
    };

    let connection_config = config.connection_config();
    match gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        default.clone(),
        f,
    ) {
        Ok((value, ServiceState::Unreachable { message })) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB connection failed: {message}");
            }
            Ok(value)
        }
        Ok((value, _)) => Ok(value),
        Err(e) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB query failed: {e}");
            }
            Ok(default)
        }
    }
}

pub fn count_callers(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = count_callers_query(&ctx.project_id, symbol_id);
        let rows = client.query(&query, Some(params))?;
        Ok(count_from_rows(&rows))
    })
}

pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = count_usages_query(&ctx.project_id, symbol_id);
        let rows = client.query(&query, Some(params))?;
        Ok(count_from_rows(&rows))
    })
}

pub fn find_callers(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_callers_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_usages(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_usages_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_callers_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_callers_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_callees_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_callees_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = get_imports_query(&ctx.project_id, file_path);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn blast_radius(
    ctx: &Context,
    symbol_id: &str,
    depth: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let query = blast_radius_query(depth, MAX_GRAPH_LIMIT);
        let params = typed_query::string_params(&[("project", &ctx.project_id), ("id", symbol_id)]);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{ProjectionProvenance, SOURCE_SYSTEM_GCODE};
    use serde_json::json;

    #[test]
    fn code_edges_carry_provenance() {
        let metadata = extracted_code_edge_metadata("src/lib.rs", 42, Some("caller-1"));

        assert_eq!(metadata.provenance, ProjectionProvenance::Extracted);
        assert_eq!(metadata.confidence, Some(1.0));
        assert_eq!(metadata.source_system, SOURCE_SYSTEM_GCODE);
        assert_eq!(metadata.source_file_path.as_deref(), Some("src/lib.rs"));
        assert_eq!(metadata.source_line, Some(42));
        assert_eq!(metadata.source_symbol_id.as_deref(), Some("caller-1"));
    }

    #[test]
    fn read_apis_return_node_link_payloads_with_link_metadata() {
        let mut payload = GraphPayload::default();
        payload.push_node(GraphNode::new("src/lib.rs", "src/lib.rs", "file"));

        let link_row = Row::from([
            ("source".to_string(), json!("src/lib.rs")),
            ("target".to_string(), json!("symbol-1")),
            ("type".to_string(), json!("DEFINES")),
            ("line".to_string(), json!(12)),
            ("provenance".to_string(), json!("EXTRACTED")),
            ("confidence".to_string(), json!(1.0)),
            ("source_system".to_string(), json!("gcode")),
            ("source_file_path".to_string(), json!("src/lib.rs")),
            ("source_line".to_string(), json!(12)),
            ("source_symbol_id".to_string(), json!("symbol-1")),
        ]);
        payload.links.push(GraphLink::from_row(&link_row));

        let encoded = serde_json::to_value(&payload).expect("payload serializes");

        assert_eq!(encoded["nodes"][0]["id"], "src/lib.rs");
        assert_eq!(encoded["nodes"][0]["type"], "file");
        assert_eq!(encoded["links"][0]["source"], "src/lib.rs");
        assert_eq!(encoded["links"][0]["target"], "symbol-1");
        assert_eq!(encoded["links"][0]["type"], "DEFINES");
        assert_eq!(encoded["links"][0]["metadata"]["provenance"], "EXTRACTED");
        assert_eq!(encoded["links"][0]["metadata"]["source_system"], "gcode");
    }

    #[test]
    fn delete_preserves_current_symbols() {
        let current_ids = vec!["symbol-current".to_string()];
        let queries =
            delete_file_graph_queries("project-1", "src/lib.rs", &current_ids).expect("queries");

        let combined = queries
            .iter()
            .map(|query| query.cypher.as_str())
            .collect::<Vec<_>>()
            .join("\n");

        assert!(
            combined.contains(
                "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})-[r:CALLS]->()"
            ),
            "{combined}"
        );
        assert!(
            combined.contains("WHERE NOT s.id IN $symbol_ids"),
            "{combined}"
        );
        assert!(
            !combined.contains(
                "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})\n                DETACH DELETE s"
            ),
            "{combined}"
        );

        let stale_symbol_cleanup = queries
            .iter()
            .find(|query| query.cypher.contains("WHERE NOT s.id IN $symbol_ids"))
            .expect("stale symbol cleanup query");
        assert_eq!(
            stale_symbol_cleanup
                .params
                .get("symbol_ids")
                .map(String::as_str),
            Some("['symbol-current']")
        );
    }

    #[test]
    fn cleanup_orphans_is_project_scoped() {
        let queries = cleanup_orphans_queries("project-1").expect("queries");
        assert_eq!(queries.len(), 3);

        for query in &queries {
            assert_eq!(
                query.params.get("project").map(String::as_str),
                Some("'project-1'")
            );
            assert!(
                query.cypher.contains("{project: $project}"),
                "{}",
                query.cypher
            );
        }

        assert!(
            queries[0]
                .cypher
                .contains("MATCH (m:CodeModule {project: $project})"),
            "{}",
            queries[0].cypher
        );
        assert!(
            queries[1]
                .cypher
                .contains("WHERE (n:UnresolvedCallee OR n:ExternalSymbol)"),
            "{}",
            queries[1].cypher
        );
        assert!(
            queries[2]
                .cypher
                .contains("MATCH (s:CodeSymbol {project: $project})")
                && queries[2].cypher.contains("s.file_path IS NULL")
                && queries[2].cypher.contains("NOT ()-[:DEFINES]->(s)")
                && queries[2].cypher.contains("NOT ()-[:CALLS]->(s)")
                && queries[2].cypher.contains("NOT (s)-[:CALLS]->()"),
            "{}",
            queries[2].cypher
        );
    }
}
