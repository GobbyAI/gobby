use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::graph::typed_query::{TypedQuery, TypedValue};
use crate::index::import_resolution::UNPARSED_IMPORT_PREFIX;
use crate::models::{
    CallRelation, CallTargetKind, ImportRelation, Symbol, make_external_symbol_id,
    make_unresolved_callee_id,
};

use super::support::{sync_token_param, typed_query, usize_value};

const EXTRACTED_PROVENANCE: &str = "EXTRACTED";
const SOURCE_SYSTEM_GCODE: &str = crate::models::SOURCE_SYSTEM_GCODE;
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

pub(super) fn new_sync_token(file_path: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let suffix = SYNC_TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{}:{}:{nanos}:{suffix}", std::process::id(), file_path)
}

#[derive(Debug, Clone)]
pub(in crate::graph::code_graph) struct ImportGraphItem {
    pub(in crate::graph::code_graph) source_file: String,
    pub(in crate::graph::code_graph) target_module: String,
}

#[derive(Debug, Clone)]
pub(in crate::graph::code_graph) struct CallGraphItem {
    caller_id: String,
    target_id: String,
    callee_name: String,
    pub(in crate::graph::code_graph) file_path: String,
    line: usize,
    callee_module: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub(in crate::graph::code_graph) struct CallGraphItems {
    pub(in crate::graph::code_graph) symbol: Vec<CallGraphItem>,
    pub(in crate::graph::code_graph) external: Vec<CallGraphItem>,
    pub(in crate::graph::code_graph) unresolved: Vec<CallGraphItem>,
}

fn map_value(values: impl IntoIterator<Item = (&'static str, TypedValue)>) -> TypedValue {
    TypedValue::Map(
        values
            .into_iter()
            .map(|(key, value)| (key.to_string(), value))
            .collect::<BTreeMap<_, _>>(),
    )
}

pub(in crate::graph::code_graph) fn import_graph_items(
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

pub(super) fn definition_graph_symbols(definitions: &[Symbol]) -> Vec<&Symbol> {
    definitions
        .iter()
        .filter(|symbol| !symbol.id.is_empty() && !symbol.name.is_empty())
        .collect()
}

pub(in crate::graph::code_graph) fn partition_call_graph_items(
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

fn append_sync_segment(cypher: &mut String, segment: &str) {
    if !cypher.is_empty() {
        cypher.push_str("\nWITH DISTINCT 1 AS _\n");
    }
    cypher.push_str(segment);
}

pub(super) struct SyncFileMutation<'a> {
    pub(super) project_id: &'a str,
    pub(super) file_path: &'a str,
    pub(super) symbol_count: usize,
    pub(super) imports: &'a [ImportGraphItem],
    pub(super) symbols: &'a [&'a Symbol],
    pub(super) calls: &'a CallGraphItems,
    pub(super) sync_token: &'a str,
}

pub(super) fn sync_file_mutation_query(input: SyncFileMutation<'_>) -> anyhow::Result<TypedQuery> {
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

pub(super) fn ensure_file_node_query(
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

pub(super) fn add_imports_query(
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

pub(super) fn add_definitions_query(
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

pub(super) fn add_symbol_calls_query(
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

pub(super) fn add_external_calls_query(
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

pub(super) fn add_unresolved_calls_query(
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
