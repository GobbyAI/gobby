//! Code-index graph projection writes.
//!
//! This is the intentional exception to the broader "Gobby-owned stores are
//! externally managed" rule: `gcode` owns the code-index graph projection and
//! writes FalkorDB `Code*` nodes/edges derived from its PostgreSQL index rows.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Context as _;

use crate::config::Context;
use crate::graph::typed_query::{TypedQuery, TypedValue};
use crate::index::import_resolution::UNPARSED_IMPORT_PREFIX;
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
static SYNC_TOKEN_COUNTER: AtomicU64 = AtomicU64::new(0);
const ADD_IMPORTS_CYPHER: &str = "UNWIND $imports AS import
         MERGE (f:CodeFile {path: import.source_file, project: $project})
         MERGE (m:CodeModule {name: import.target_module, project: $project})
         MERGE (f)-[r:IMPORTS]->(m)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = import.source_file,
             r.sync_token = $sync_token";
const ADD_DEFINITIONS_CYPHER: &str = "UNWIND $symbols AS symbol
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
             r.sync_token = $sync_token";
const ADD_SYMBOL_CALLS_CYPHER: &str = "UNWIND $symbol_calls AS call
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
             r.sync_token = $sync_token";
const ADD_EXTERNAL_CALLS_CYPHER: &str = "UNWIND $external_calls AS call
         MERGE (caller:CodeSymbol {id: call.caller_id, project: $project})
         MERGE (callee:ExternalSymbol {id: call.target_id, project: $project})
         ON CREATE SET callee.name = call.callee_name,
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
             r.sync_token = $sync_token";
const ADD_UNRESOLVED_CALLS_CYPHER: &str = "UNWIND $unresolved_calls AS call
         MERGE (caller:CodeSymbol {id: call.caller_id, project: $project})
         MERGE (callee:UnresolvedCallee {id: call.target_id, project: $project})
         ON CREATE SET callee.name = call.callee_name,
             callee.updated_at = timestamp(),
             callee.sync_token = $sync_token
         MERGE (caller)-[r:CALLS {file: call.file_path, line: call.line}]->(callee)
         SET r.provenance = $provenance,
             r.confidence = $confidence,
             r.source_system = $source_system,
             r.source_file_path = call.file_path,
             r.source_line = call.line,
             r.source_symbol_id = call.caller_id,
             r.sync_token = $sync_token";

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
        let sync_token = new_sync_token(file_path);
        let import_items = import_graph_items(file_path, imports);
        let symbols = definition_graph_symbols(definitions);
        let current_symbol_ids = symbols
            .iter()
            .map(|symbol| symbol.id.clone())
            .collect::<Vec<_>>();
        let call_groups = partition_call_graph_items(self.project_id, file_path, calls);
        let relationship_count = import_items.len()
            + symbols.len()
            + call_groups.symbol.len()
            + call_groups.external.len()
            + call_groups.unresolved.len();
        execute_write_query(
            self.client,
            sync_file_mutation_query(SyncFileMutation {
                project_id: self.project_id,
                file_path,
                symbol_count: definitions.len(),
                imports: &import_items,
                symbols: &symbols,
                calls: &call_groups,
                sync_token: &sync_token,
            })?,
        )?;
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
        let items = import_graph_items(file_path, imports);
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
        let call_groups = partition_call_graph_items(self.project_id, file_path, calls);

        let mut written = 0;
        if !call_groups.symbol.is_empty() {
            written += call_groups.symbol.len();
            execute_write_query(
                self.client,
                add_symbol_calls_query(self.project_id, &call_groups.symbol, sync_token)?,
            )?;
        }
        if !call_groups.external.is_empty() {
            written += call_groups.external.len();
            execute_write_query(
                self.client,
                add_external_calls_query(self.project_id, &call_groups.external, sync_token)?,
            )?;
        }
        if !call_groups.unresolved.is_empty() {
            written += call_groups.unresolved.len();
            execute_write_query(
                self.client,
                add_unresolved_calls_query(self.project_id, &call_groups.unresolved, sync_token)?,
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
    with_code_graph(ctx, |graph| {
        graph.sync_file(file_path, imports, definitions, calls, cleanup_orphans)
    })
}

pub fn with_code_graph<T>(
    ctx: &Context,
    f: impl FnOnce(&mut CodeGraph<'_>) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    with_required_core_graph(ctx, |client| {
        let mut graph = CodeGraph::new(&ctx.project_id, client);
        graph.ensure_project_indexes()?;
        f(&mut graph)
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
    with_code_graph(ctx, |graph| graph.cleanup_orphans())
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
    let suffix = SYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{}:{}:{nanos}:{suffix}", std::process::id(), file_path)
}

fn typed_query<I, K>(cypher: impl Into<String>, params: I) -> anyhow::Result<TypedQuery>
where
    I: IntoIterator<Item = (K, TypedValue)>,
    K: Into<String>,
{
    Ok(TypedQuery::with_params(cypher, params)?)
}

fn usize_value(value: usize) -> anyhow::Result<TypedValue> {
    Ok(TypedValue::Integer(i64::try_from(value).context(
        "graph integer value exceeds FalkorDB i64 range",
    )?))
}

#[derive(Debug, Clone)]
pub(super) struct ImportGraphItem {
    pub(super) source_file: String,
    pub(super) target_module: String,
}

#[derive(Debug, Clone)]
pub(super) struct CallGraphItem {
    caller_id: String,
    target_id: String,
    callee_name: String,
    pub(super) file_path: String,
    line: usize,
    callee_module: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub(super) struct CallGraphItems {
    symbol: Vec<CallGraphItem>,
    external: Vec<CallGraphItem>,
    pub(super) unresolved: Vec<CallGraphItem>,
}

fn map_value(values: impl IntoIterator<Item = (&'static str, TypedValue)>) -> TypedValue {
    TypedValue::Map(
        values
            .into_iter()
            .map(|(key, value)| (key.to_string(), value))
            .collect::<BTreeMap<_, _>>(),
    )
}

pub(super) fn import_graph_items(
    file_path: &str,
    imports: &[ImportRelation],
) -> Vec<ImportGraphItem> {
    imports
        .iter()
        .filter(|import| {
            !import.module_name.is_empty()
                && !import.module_name.starts_with(UNPARSED_IMPORT_PREFIX)
        })
        .map(|import| ImportGraphItem {
            source_file: file_path.to_string(),
            target_module: import.module_name.clone(),
        })
        .collect()
}

fn definition_graph_symbols(definitions: &[Symbol]) -> Vec<&Symbol> {
    definitions
        .iter()
        .filter(|symbol| !symbol.id.is_empty() && !symbol.name.is_empty())
        .collect()
}

pub(super) fn partition_call_graph_items(
    project_id: &str,
    file_path: &str,
    calls: &[CallRelation],
) -> CallGraphItems {
    let mut groups = CallGraphItems::default();
    for call in calls {
        if call.caller_symbol_id.is_empty() {
            continue;
        }
        let Some(target) = GraphCallTarget::from_call(project_id, call) else {
            continue;
        };
        let item = CallGraphItem {
            caller_id: call.caller_symbol_id.clone(),
            target_id: target.id().to_string(),
            callee_name: call.callee_name.clone(),
            file_path: file_path.to_string(),
            line: call.line,
            callee_module: target.module().map(str::to_string),
        };
        match target {
            GraphCallTarget::Symbol { .. } => groups.symbol.push(item),
            GraphCallTarget::External { .. } => groups.external.push(item),
            GraphCallTarget::Unresolved { .. } => groups.unresolved.push(item),
        }
    }
    groups
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

fn append_sync_segment(cypher: &mut String, segment: &str) {
    if !cypher.is_empty() {
        cypher.push_str("\nWITH DISTINCT 1 AS _\n");
    }
    cypher.push_str(segment);
}

struct SyncFileMutation<'a> {
    project_id: &'a str,
    file_path: &'a str,
    symbol_count: usize,
    imports: &'a [ImportGraphItem],
    symbols: &'a [&'a Symbol],
    calls: &'a CallGraphItems,
    sync_token: &'a str,
}

fn sync_file_mutation_query(input: SyncFileMutation<'_>) -> anyhow::Result<TypedQuery> {
    let mut cypher = String::new();
    append_sync_segment(
        &mut cypher,
        "MERGE (f:CodeFile {path: $file_path, project: $project})
         SET f.updated_at = timestamp(),
             f.symbol_count = $symbol_count,
             f.sync_token = $sync_token",
    );
    if !input.imports.is_empty() {
        append_sync_segment(&mut cypher, ADD_IMPORTS_CYPHER);
    }
    if !input.symbols.is_empty() {
        append_sync_segment(&mut cypher, ADD_DEFINITIONS_CYPHER);
    }
    if !input.calls.symbol.is_empty() {
        append_sync_segment(&mut cypher, ADD_SYMBOL_CALLS_CYPHER);
    }
    if !input.calls.external.is_empty() {
        append_sync_segment(&mut cypher, ADD_EXTERNAL_CALLS_CYPHER);
    }
    if !input.calls.unresolved.is_empty() {
        append_sync_segment(&mut cypher, ADD_UNRESOLVED_CALLS_CYPHER);
    }
    let mut params = vec![
        ("project", TypedValue::String(input.project_id.to_string())),
        ("file_path", TypedValue::String(input.file_path.to_string())),
        ("symbol_count", usize_value(input.symbol_count)?),
        ("imports", import_rows(input.imports)),
        ("symbols", symbol_rows(input.symbols)?),
        ("symbol_calls", call_rows(&input.calls.symbol)?),
        ("external_calls", call_rows(&input.calls.external)?),
        ("unresolved_calls", call_rows(&input.calls.unresolved)?),
    ];
    params.extend(metadata_params(input.sync_token));
    typed_query(cypher, params)
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
            ("symbol_count", usize_value(symbol_count)?),
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
    typed_query(ADD_IMPORTS_CYPHER, params)
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
                        Ok(map_value([
                            ("id", TypedValue::String(symbol.id.clone())),
                            ("name", TypedValue::String(symbol.name.clone())),
                            (
                                "qualified_name",
                                TypedValue::String(symbol.qualified_name.clone()),
                            ),
                            ("kind", TypedValue::String(symbol.kind.clone())),
                            ("language", TypedValue::String(symbol.language.clone())),
                            ("line_start", usize_value(symbol.line_start)?),
                            ("line_end", usize_value(symbol.line_end)?),
                        ]))
                    })
                    .collect::<anyhow::Result<Vec<_>>>()?,
            ),
        ),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(ADD_DEFINITIONS_CYPHER, params)
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

fn call_rows(calls: &[CallGraphItem]) -> anyhow::Result<TypedValue> {
    Ok(TypedValue::List(
        calls
            .iter()
            .map(|call| {
                Ok(map_value([
                    ("caller_id", TypedValue::String(call.caller_id.clone())),
                    ("target_id", TypedValue::String(call.target_id.clone())),
                    ("callee_name", TypedValue::String(call.callee_name.clone())),
                    ("file_path", TypedValue::String(call.file_path.clone())),
                    ("line", usize_value(call.line)?),
                    (
                        "callee_module",
                        TypedValue::String(call.callee_module.clone().unwrap_or_default()),
                    ),
                ]))
            })
            .collect::<anyhow::Result<Vec<_>>>()?,
    ))
}

fn import_rows(imports: &[ImportGraphItem]) -> TypedValue {
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
    )
}

fn symbol_rows(symbols: &[&Symbol]) -> anyhow::Result<TypedValue> {
    Ok(TypedValue::List(
        symbols
            .iter()
            .map(|symbol| {
                Ok(map_value([
                    ("id", TypedValue::String(symbol.id.clone())),
                    ("name", TypedValue::String(symbol.name.clone())),
                    (
                        "qualified_name",
                        TypedValue::String(symbol.qualified_name.clone()),
                    ),
                    ("kind", TypedValue::String(symbol.kind.clone())),
                    ("language", TypedValue::String(symbol.language.clone())),
                    ("line_start", usize_value(symbol.line_start)?),
                    ("line_end", usize_value(symbol.line_end)?),
                ]))
            })
            .collect::<anyhow::Result<Vec<_>>>()?,
    ))
}

fn add_symbol_calls_query(
    project_id: &str,
    calls: &[CallGraphItem],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("symbol_calls", call_rows(calls)?),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(ADD_SYMBOL_CALLS_CYPHER, params)
}

fn add_external_calls_query(
    project_id: &str,
    calls: &[CallGraphItem],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("external_calls", call_rows(calls)?),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(ADD_EXTERNAL_CALLS_CYPHER, params)
}

fn add_unresolved_calls_query(
    project_id: &str,
    calls: &[CallGraphItem],
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("unresolved_calls", call_rows(calls)?),
    ];
    params.extend(metadata_params(sync_token));
    typed_query(ADD_UNRESOLVED_CALLS_CYPHER, params)
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
