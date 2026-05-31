use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::config::Context;
use crate::graph::typed_query::{TypedQuery, TypedValue};
use crate::models::{
    CallRelation, CallTargetKind, ImportRelation, Symbol, make_external_symbol_id,
    make_unresolved_callee_id,
};
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::GraphClient;

use super::GraphReadError;
use super::connection::with_required_core_graph;

const PROJECT_NODE_PREDICATE: &str =
    "n:CodeFile OR n:CodeSymbol OR n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol";
const EXTRACTED_PROVENANCE: &str = "EXTRACTED";
const SOURCE_SYSTEM_GCODE: &str = crate::models::SOURCE_SYSTEM_GCODE;
const PROJECT_INDEXED_LABELS: &[&str] = &[
    "CodeFile",
    "CodeSymbol",
    "CodeModule",
    "UnresolvedCallee",
    "ExternalSymbol",
];

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
        cleanup_orphans: bool,
    ) -> anyhow::Result<usize> {
        self.ensure_project_indexes()?;
        let sync_token = new_sync_token(file_path);
        self.ensure_file_node(file_path, definitions.len(), &sync_token)?;
        let current_symbol_ids = definitions
            .iter()
            .map(|symbol| symbol.id.clone())
            .collect::<Vec<_>>();

        let mut relationship_count = 0;
        relationship_count += self.add_imports(file_path, imports, &sync_token)?;
        relationship_count += self.add_definitions(file_path, definitions, &sync_token)?;
        relationship_count += self.add_calls(file_path, calls, &sync_token)?;
        self.delete_stale_file_graph(file_path, &current_symbol_ids, &sync_token)?;
        if cleanup_orphans {
            self.cleanup_orphans()?;
        }
        Ok(relationship_count)
    }

    pub fn ensure_project_indexes(&mut self) -> anyhow::Result<()> {
        for label in PROJECT_INDEXED_LABELS {
            self.client.ensure_exact_node_index(label, "project")?;
        }
        Ok(())
    }

    pub fn ensure_file_node(
        &mut self,
        file_path: &str,
        symbol_count: usize,
        sync_token: &str,
    ) -> anyhow::Result<()> {
        execute_write_query(
            self.client,
            ensure_file_node_query(self.project_id, file_path, symbol_count, sync_token)?,
        )
    }

    pub fn add_imports(
        &mut self,
        file_path: &str,
        imports: &[ImportRelation],
        sync_token: &str,
    ) -> anyhow::Result<usize> {
        let items = imports
            .iter()
            .filter(|import| !import.module_name.is_empty())
            .map(|import| ImportGraphItem {
                source_file: if import.file_path.is_empty() {
                    file_path.to_string()
                } else {
                    import.file_path.clone()
                },
                target_module: import.module_name.clone(),
            })
            .collect::<Vec<_>>();
        if items.is_empty() {
            return Ok(0);
        }
        let written = items.len();
        execute_write_query(
            self.client,
            add_imports_query(self.project_id, &items, sync_token)?,
        )?;
        Ok(written)
    }

    pub fn add_definitions(
        &mut self,
        file_path: &str,
        definitions: &[Symbol],
        sync_token: &str,
    ) -> anyhow::Result<usize> {
        let symbols = definitions
            .iter()
            .filter(|symbol| !symbol.id.is_empty() && !symbol.name.is_empty())
            .collect::<Vec<_>>();
        if symbols.is_empty() {
            return Ok(0);
        }
        let written = symbols.len();
        execute_write_query(
            self.client,
            add_definitions_query(self.project_id, file_path, &symbols, sync_token)?,
        )?;
        Ok(written)
    }

    pub fn add_calls(
        &mut self,
        file_path: &str,
        calls: &[CallRelation],
        sync_token: &str,
    ) -> anyhow::Result<usize> {
        let mut symbol_calls = Vec::new();
        let mut external_calls = Vec::new();
        let mut unresolved_calls = Vec::new();
        for call in calls {
            if call.caller_symbol_id.is_empty() {
                continue;
            }
            let Some(target) = GraphCallTarget::from_call(self.project_id, call) else {
                continue;
            };
            let call_file_path = if call.file_path.is_empty() {
                file_path.to_string()
            } else {
                call.file_path.clone()
            };
            let item = CallGraphItem {
                caller_id: call.caller_symbol_id.clone(),
                target_id: target.id().to_string(),
                callee_name: call.callee_name.clone(),
                file_path: call_file_path,
                line: call.line,
                callee_module: target.module().map(str::to_string),
            };
            match target {
                GraphCallTarget::Symbol { .. } => symbol_calls.push(item),
                GraphCallTarget::External { .. } => external_calls.push(item),
                GraphCallTarget::Unresolved { .. } => unresolved_calls.push(item),
            }
        }

        let mut written = 0;
        if !symbol_calls.is_empty() {
            written += symbol_calls.len();
            execute_write_query(
                self.client,
                add_symbol_calls_query(self.project_id, &symbol_calls, sync_token)?,
            )?;
        }
        if !external_calls.is_empty() {
            written += external_calls.len();
            execute_write_query(
                self.client,
                add_external_calls_query(self.project_id, &external_calls, sync_token)?,
            )?;
        }
        if !unresolved_calls.is_empty() {
            written += unresolved_calls.len();
            execute_write_query(
                self.client,
                add_unresolved_calls_query(self.project_id, &unresolved_calls, sync_token)?,
            )?;
        }
        Ok(written)
    }

    pub fn delete_stale_file_graph(
        &mut self,
        file_path: &str,
        current_symbol_ids: &[String],
        sync_token: &str,
    ) -> anyhow::Result<()> {
        for query in delete_stale_file_graph_queries(
            self.project_id,
            file_path,
            current_symbol_ids,
            sync_token,
        )? {
            execute_write_query(self.client, query)?;
        }
        Ok(())
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

    pub fn delete_file_node(&mut self, file_path: &str) -> anyhow::Result<()> {
        execute_write_query(
            self.client,
            delete_file_node_query(self.project_id, file_path)?,
        )
    }

    pub fn delete_file_projection(&mut self, file_path: &str) -> anyhow::Result<()> {
        self.delete_file_graph(file_path, &[])?;
        self.delete_file_node(file_path)?;
        self.cleanup_orphans()
    }

    pub fn cleanup_orphans(&mut self) -> anyhow::Result<()> {
        self.ensure_project_indexes()?;
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
    cleanup_orphans: bool,
) -> anyhow::Result<usize> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).sync_file(
            file_path,
            imports,
            definitions,
            calls,
            cleanup_orphans,
        )
    })
}

pub fn delete_file_graph(
    ctx: &Context,
    file_path: &str,
    current_symbol_ids: &[String],
) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).delete_file_graph(file_path, current_symbol_ids)
    })
}

pub fn delete_file_projection(ctx: &Context, file_path: &str) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).delete_file_projection(file_path)
    })
}

pub fn cleanup_orphans(ctx: &Context) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).cleanup_orphans()
    })
}

pub fn clear_project(ctx: &Context) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).clear_project()
    })
}

pub fn clear_all_code_index(config: &crate::config::FalkorConfig) -> anyhow::Result<()> {
    let connection_config = config.connection_config();
    match gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        None,
        |client| execute_write_query(client, clear_all_code_index_query()?).map(Some),
    ) {
        Ok((Some(()), ServiceState::Available)) => Ok(()),
        Ok((_, ServiceState::NotConfigured)) => Err(GraphReadError::NotConfigured.into()),
        Ok((_, ServiceState::Unreachable { message })) => {
            log::warn!("FalkorDB was unreachable while clearing code graph: {message}");
            Err(GraphReadError::Unreachable { message }.into())
        }
        Ok((None, ServiceState::Available)) => Err(GraphReadError::QueryFailed {
            message: "graph clear returned no value".to_string(),
        }
        .into()),
        Err(error) => Err(GraphReadError::QueryFailed {
            message: error.to_string(),
        }
        .into()),
    }
}

fn execute_write_query(client: &mut GraphClient, query: TypedQuery) -> anyhow::Result<()> {
    let TypedQuery { cypher, params } = query;
    client.query(&cypher, Some(params))?;
    Ok(())
}

fn new_sync_token(file_path: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    format!("{}:{}:{nanos}", std::process::id(), file_path)
}

fn typed_query<I, K>(cypher: impl Into<String>, params: I) -> anyhow::Result<TypedQuery>
where
    I: IntoIterator<Item = (K, TypedValue)>,
    K: Into<String>,
{
    Ok(TypedQuery::with_params(cypher, params)?)
}

fn usize_value(value: usize) -> TypedValue {
    TypedValue::Integer(i64::try_from(value).unwrap_or(i64::MAX))
}

#[derive(Debug, Clone)]
struct ImportGraphItem {
    source_file: String,
    target_module: String,
}

#[derive(Debug, Clone)]
struct CallGraphItem {
    caller_id: String,
    target_id: String,
    callee_name: String,
    file_path: String,
    line: usize,
    callee_module: Option<String>,
}

fn map_value(values: impl IntoIterator<Item = (&'static str, TypedValue)>) -> TypedValue {
    TypedValue::Map(
        values
            .into_iter()
            .map(|(key, value)| (key.to_string(), value))
            .collect::<BTreeMap<_, _>>(),
    )
}

fn metadata_params(sync_token: &str) -> Vec<(&'static str, TypedValue)> {
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
        sync_token_param(sync_token),
    ]
}

fn sync_token_param(sync_token: &str) -> (&'static str, TypedValue) {
    ("sync_token", TypedValue::String(sync_token.to_string()))
}

pub(crate) fn ensure_file_node_query(
    project_id: &str,
    file_path: &str,
    symbol_count: usize,
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    typed_query(
        "MERGE (f:CodeFile {path: $file_path, project: $project})
         SET f.updated_at = timestamp(),
             f.symbol_count = $symbol_count,
             f.sync_token = $sync_token",
        [
            ("project", TypedValue::String(project_id.to_string())),
            ("file_path", TypedValue::String(file_path.to_string())),
            ("symbol_count", usize_value(symbol_count)),
            sync_token_param(sync_token),
        ],
    )
}

fn add_imports_query(
    project_id: &str,
    imports: &[ImportGraphItem],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        (
            "imports",
            TypedValue::List(
                imports
                    .iter()
                    .map(|import| {
                        map_value([
                            (
                                "source_file",
                                TypedValue::String(import.source_file.clone()),
                            ),
                            (
                                "target_module",
                                TypedValue::String(import.target_module.clone()),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(
        "UNWIND $imports AS import
         MERGE (f:CodeFile {path: import.source_file, project: $project})
         MERGE (m:CodeModule {name: import.target_module, project: $project})
         MERGE (f)-[r:IMPORTS]->(m)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = import.source_file,
             r.sync_token = $sync_token",
        params,
    )
}

fn add_definitions_query(
    project_id: &str,
    file_path: &str,
    symbols: &[&Symbol],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("file_path", TypedValue::String(file_path.to_string())),
        (
            "symbols",
            TypedValue::List(
                symbols
                    .iter()
                    .map(|symbol| {
                        map_value([
                            ("id", TypedValue::String(symbol.id.clone())),
                            ("name", TypedValue::String(symbol.name.clone())),
                            (
                                "qualified_name",
                                TypedValue::String(symbol.qualified_name.clone()),
                            ),
                            ("kind", TypedValue::String(symbol.kind.clone())),
                            ("language", TypedValue::String(symbol.language.clone())),
                            ("line_start", usize_value(symbol.line_start)),
                            ("line_end", usize_value(symbol.line_end)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(
        "UNWIND $symbols AS symbol
         MERGE (f:CodeFile {path: $file_path, project: $project})
         MERGE (s:CodeSymbol {id: symbol.id, project: $project})
         SET s.name = symbol.name,
             s.qualified_name = symbol.qualified_name,
             s.kind = symbol.kind,
             s.language = symbol.language,
             s.file_path = $file_path,
             s.line_start = symbol.line_start,
             s.line_end = symbol.line_end,
             s.updated_at = timestamp(),
             s.sync_token = $sync_token
         MERGE (f)-[r:DEFINES]->(s)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = $file_path,
             r.source_line = symbol.line_start,
             r.source_symbol_id = symbol.id,
             r.sync_token = $sync_token",
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

    fn id(&self) -> &str {
        match self {
            Self::Symbol { id } | Self::External { id, .. } | Self::Unresolved { id } => id,
        }
    }

    fn module(&self) -> Option<&str> {
        match self {
            Self::External { module, .. } => Some(module),
            Self::Symbol { .. } | Self::Unresolved { .. } => None,
        }
    }
}

pub fn call_target_id(project_id: &str, call: &CallRelation) -> Option<String> {
    match GraphCallTarget::from_call(project_id, call)? {
        GraphCallTarget::Symbol { id }
        | GraphCallTarget::External { id, .. }
        | GraphCallTarget::Unresolved { id } => Some(id),
    }
}

fn call_rows(calls: &[CallGraphItem]) -> TypedValue {
    TypedValue::List(
        calls
            .iter()
            .map(|call| {
                map_value([
                    ("caller_id", TypedValue::String(call.caller_id.clone())),
                    ("target_id", TypedValue::String(call.target_id.clone())),
                    ("callee_name", TypedValue::String(call.callee_name.clone())),
                    ("file_path", TypedValue::String(call.file_path.clone())),
                    ("line", usize_value(call.line)),
                    (
                        "callee_module",
                        TypedValue::String(call.callee_module.clone().unwrap_or_default()),
                    ),
                ])
            })
            .collect(),
    )
}

fn add_symbol_calls_query(
    project_id: &str,
    calls: &[CallGraphItem],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("calls", call_rows(calls)),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(
        "UNWIND $calls AS call
         MERGE (caller:CodeSymbol {id: call.caller_id, project: $project})
         MERGE (callee:CodeSymbol {id: call.target_id, project: $project})
         ON CREATE SET callee.name = call.callee_name, callee.updated_at = timestamp()
         MERGE (caller)-[r:CALLS {file: call.file_path, line: call.line}]->(callee)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = call.file_path,
             r.source_line = call.line,
             r.source_symbol_id = call.caller_id,
             r.sync_token = $sync_token",
        params,
    )
}

fn add_external_calls_query(
    project_id: &str,
    calls: &[CallGraphItem],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("calls", call_rows(calls)),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(
        "UNWIND $calls AS call
         MERGE (caller:CodeSymbol {id: call.caller_id, project: $project})
         MERGE (callee:ExternalSymbol {id: call.target_id, project: $project})
         SET callee.name = call.callee_name,
             callee.external_module = call.callee_module,
             callee.module = call.callee_module,
             callee.updated_at = timestamp(),
             callee.sync_token = $sync_token
         MERGE (caller)-[r:CALLS {file: call.file_path, line: call.line}]->(callee)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = call.file_path,
             r.source_line = call.line,
             r.source_symbol_id = call.caller_id,
             r.sync_token = $sync_token",
        params,
    )
}

fn add_unresolved_calls_query(
    project_id: &str,
    calls: &[CallGraphItem],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("calls", call_rows(calls)),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(
        "UNWIND $calls AS call
         MERGE (caller:CodeSymbol {id: call.caller_id, project: $project})
         MERGE (callee:UnresolvedCallee {id: call.target_id, project: $project})
         SET callee.name = call.callee_name,
             callee.updated_at = timestamp(),
             callee.sync_token = $sync_token
         MERGE (caller)-[r:CALLS {file: call.file_path, line: call.line}]->(callee)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = call.file_path,
             r.source_line = call.line,
             r.source_symbol_id = call.caller_id,
             r.sync_token = $sync_token",
        params,
    )
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

pub(crate) fn delete_stale_file_graph_queries(
    project_id: &str,
    file_path: &str,
    current_symbol_ids: &[String],
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

    let mut symbol_params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("file_path", TypedValue::String(file_path.to_string())),
        sync_token_param(sync_token),
    ];
    if current_symbol_ids.is_empty() {
        queries.push(typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
             WHERE s.sync_token IS NULL OR s.sync_token <> $sync_token
             DETACH DELETE s",
            symbol_params,
        )?);
    } else {
        symbol_params.push((
            "symbol_ids",
            TypedValue::List(
                current_symbol_ids
                    .iter()
                    .map(|id| TypedValue::String(id.clone()))
                    .collect(),
            ),
        ));
        queries.push(typed_query(
            "MATCH (s:CodeSymbol {project: $project, file_path: $file_path})
             WHERE (s.sync_token IS NULL OR s.sync_token <> $sync_token)
               AND NOT s.id IN $symbol_ids
             DETACH DELETE s",
            symbol_params,
        )?);
    }

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

pub(crate) fn cleanup_orphans_queries(project_id: &str) -> anyhow::Result<Vec<TypedQuery>> {
    let project_param = || [("project", TypedValue::String(project_id.to_string()))];
    // Orphan cleanup runs after low-activity sync paths so failed writes leave
    // the previous projection available.
    Ok(vec![
        typed_query(
            "MATCH (m:CodeModule {project: $project})
             WHERE NOT (:CodeFile {project: $project})-[:IMPORTS]->(m)
             DETACH DELETE m",
            project_param(),
        )?,
        typed_query(
            "MATCH (n {project: $project})
             WHERE (n:UnresolvedCallee OR n:ExternalSymbol)
               AND NOT ({project: $project})-[:CALLS]->(n)
             DETACH DELETE n",
            project_param(),
        )?,
        typed_query(
            "MATCH (s:CodeSymbol {project: $project})
             WHERE s.file_path IS NULL
               AND NOT (:CodeFile {project: $project})-[:DEFINES]->(s)
               AND NOT ({project: $project})-[:CALLS]->(s)
               AND NOT (s)-[:CALLS]->({project: $project})
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
