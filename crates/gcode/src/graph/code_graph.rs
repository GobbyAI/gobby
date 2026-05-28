use std::collections::HashMap;

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
const PROJECT_NODE_PREDICATE: &str =
    "n:CodeFile OR n:CodeSymbol OR n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol";
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

fn count_from_rows(rows: &[Row]) -> usize {
    rows.first()
        .and_then(|r| r.get("cnt"))
        .and_then(|v| {
            v.as_u64()
                .or_else(|| v.as_i64().and_then(|value| value.try_into().ok()))
        })
        .unwrap_or(0) as usize
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
    with_core_graph(ctx, 0, |client| {
        let (query, params) = count_callers_query(&ctx.project_id, symbol_id);
        let rows = client.query(&query, Some(params))?;
        Ok(count_from_rows(&rows))
    })
}

pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_core_graph(ctx, 0, |client| {
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
    with_core_graph(ctx, vec![], |client| {
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
    with_core_graph(ctx, vec![], |client| {
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
    with_core_graph(ctx, vec![], |client| {
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
    with_core_graph(ctx, vec![], |client| {
        let (query, params) = find_callees_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    with_core_graph(ctx, vec![], |client| {
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
    with_core_graph(ctx, vec![], |client| {
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
