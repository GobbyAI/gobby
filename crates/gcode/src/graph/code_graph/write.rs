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
        if cleanup_orphans {
            self.cleanup_orphans()?;
        }
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
