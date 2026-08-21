use std::collections::BTreeMap;

use crate::graph::typed_query::{TypedQuery, TypedValue};
use crate::models::{
    CallTargetKind, HeritageKind, InheritanceRelation, make_external_symbol_id,
    make_unresolved_callee_id,
};

use super::mutation::{
    ADD_EXTENDS_EXTERNAL_EXTERNAL_CYPHER, ADD_EXTENDS_EXTERNAL_SYMBOL_CYPHER,
    ADD_EXTENDS_EXTERNAL_UNRESOLVED_CYPHER, ADD_EXTENDS_SYMBOL_EXTERNAL_CYPHER,
    ADD_EXTENDS_SYMBOL_SYMBOL_CYPHER, ADD_EXTENDS_SYMBOL_UNRESOLVED_CYPHER,
    ADD_EXTENDS_UNRESOLVED_EXTERNAL_CYPHER, ADD_EXTENDS_UNRESOLVED_SYMBOL_CYPHER,
    ADD_EXTENDS_UNRESOLVED_UNRESOLVED_CYPHER, ADD_IMPLEMENTS_EXTERNAL_EXTERNAL_CYPHER,
    ADD_IMPLEMENTS_EXTERNAL_SYMBOL_CYPHER, ADD_IMPLEMENTS_EXTERNAL_UNRESOLVED_CYPHER,
    ADD_IMPLEMENTS_SYMBOL_EXTERNAL_CYPHER, ADD_IMPLEMENTS_SYMBOL_SYMBOL_CYPHER,
    ADD_IMPLEMENTS_SYMBOL_UNRESOLVED_CYPHER, ADD_IMPLEMENTS_UNRESOLVED_EXTERNAL_CYPHER,
    ADD_IMPLEMENTS_UNRESOLVED_SYMBOL_CYPHER, ADD_IMPLEMENTS_UNRESOLVED_UNRESOLVED_CYPHER,
    ADD_INHERITS_EXTERNAL_EXTERNAL_CYPHER, ADD_INHERITS_EXTERNAL_SYMBOL_CYPHER,
    ADD_INHERITS_EXTERNAL_UNRESOLVED_CYPHER, ADD_INHERITS_SYMBOL_EXTERNAL_CYPHER,
    ADD_INHERITS_SYMBOL_SYMBOL_CYPHER, ADD_INHERITS_SYMBOL_UNRESOLVED_CYPHER,
    ADD_INHERITS_UNRESOLVED_EXTERNAL_CYPHER, ADD_INHERITS_UNRESOLVED_SYMBOL_CYPHER,
    ADD_INHERITS_UNRESOLVED_UNRESOLVED_CYPHER, map_value, metadata_params,
};
use super::support::{typed_query, usize_value};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(super) enum HeritageEndpoint {
    Symbol,
    External,
    Unresolved,
}

fn heritage_cypher(
    kind: HeritageKind,
    source: HeritageEndpoint,
    target: HeritageEndpoint,
) -> &'static str {
    match (kind, source, target) {
        (HeritageKind::Inherits, HeritageEndpoint::Symbol, HeritageEndpoint::Symbol) => {
            ADD_INHERITS_SYMBOL_SYMBOL_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::Symbol, HeritageEndpoint::External) => {
            ADD_INHERITS_SYMBOL_EXTERNAL_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::Symbol, HeritageEndpoint::Unresolved) => {
            ADD_INHERITS_SYMBOL_UNRESOLVED_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::External, HeritageEndpoint::Symbol) => {
            ADD_INHERITS_EXTERNAL_SYMBOL_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::External, HeritageEndpoint::External) => {
            ADD_INHERITS_EXTERNAL_EXTERNAL_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::External, HeritageEndpoint::Unresolved) => {
            ADD_INHERITS_EXTERNAL_UNRESOLVED_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::Unresolved, HeritageEndpoint::Symbol) => {
            ADD_INHERITS_UNRESOLVED_SYMBOL_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::Unresolved, HeritageEndpoint::External) => {
            ADD_INHERITS_UNRESOLVED_EXTERNAL_CYPHER
        }
        (HeritageKind::Inherits, HeritageEndpoint::Unresolved, HeritageEndpoint::Unresolved) => {
            ADD_INHERITS_UNRESOLVED_UNRESOLVED_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::Symbol, HeritageEndpoint::Symbol) => {
            ADD_EXTENDS_SYMBOL_SYMBOL_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::Symbol, HeritageEndpoint::External) => {
            ADD_EXTENDS_SYMBOL_EXTERNAL_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::Symbol, HeritageEndpoint::Unresolved) => {
            ADD_EXTENDS_SYMBOL_UNRESOLVED_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::External, HeritageEndpoint::Symbol) => {
            ADD_EXTENDS_EXTERNAL_SYMBOL_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::External, HeritageEndpoint::External) => {
            ADD_EXTENDS_EXTERNAL_EXTERNAL_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::External, HeritageEndpoint::Unresolved) => {
            ADD_EXTENDS_EXTERNAL_UNRESOLVED_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::Unresolved, HeritageEndpoint::Symbol) => {
            ADD_EXTENDS_UNRESOLVED_SYMBOL_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::Unresolved, HeritageEndpoint::External) => {
            ADD_EXTENDS_UNRESOLVED_EXTERNAL_CYPHER
        }
        (HeritageKind::Extends, HeritageEndpoint::Unresolved, HeritageEndpoint::Unresolved) => {
            ADD_EXTENDS_UNRESOLVED_UNRESOLVED_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::Symbol, HeritageEndpoint::Symbol) => {
            ADD_IMPLEMENTS_SYMBOL_SYMBOL_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::Symbol, HeritageEndpoint::External) => {
            ADD_IMPLEMENTS_SYMBOL_EXTERNAL_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::Symbol, HeritageEndpoint::Unresolved) => {
            ADD_IMPLEMENTS_SYMBOL_UNRESOLVED_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::External, HeritageEndpoint::Symbol) => {
            ADD_IMPLEMENTS_EXTERNAL_SYMBOL_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::External, HeritageEndpoint::External) => {
            ADD_IMPLEMENTS_EXTERNAL_EXTERNAL_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::External, HeritageEndpoint::Unresolved) => {
            ADD_IMPLEMENTS_EXTERNAL_UNRESOLVED_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::Unresolved, HeritageEndpoint::Symbol) => {
            ADD_IMPLEMENTS_UNRESOLVED_SYMBOL_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::Unresolved, HeritageEndpoint::External) => {
            ADD_IMPLEMENTS_UNRESOLVED_EXTERNAL_CYPHER
        }
        (HeritageKind::Implements, HeritageEndpoint::Unresolved, HeritageEndpoint::Unresolved) => {
            ADD_IMPLEMENTS_UNRESOLVED_UNRESOLVED_CYPHER
        }
    }
}

#[derive(Debug, Clone)]
pub(super) struct HeritageGraphItem {
    source_id: String,
    target_id: String,
    source_name: String,
    target_name: String,
    file_path: String,
    line: usize,
    source_module: Option<String>,
    target_module: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub(super) struct InheritanceGraphItems {
    groups: BTreeMap<(HeritageEndpoint, HeritageEndpoint, HeritageKind), Vec<HeritageGraphItem>>,
}

impl InheritanceGraphItems {
    pub(super) fn row_count(&self) -> usize {
        self.groups.values().map(Vec::len).sum()
    }

    pub(super) fn iter_non_empty(
        &self,
    ) -> impl Iterator<
        Item = (
            HeritageEndpoint,
            HeritageEndpoint,
            HeritageKind,
            &[HeritageGraphItem],
        ),
    > {
        self.groups
            .iter()
            .filter_map(|((source, target, kind), rows)| {
                (!rows.is_empty()).then_some((*source, *target, *kind, rows.as_slice()))
            })
    }
}

pub(super) fn partition_inheritance_graph_items(
    project_id: &str,
    file_path: &str,
    inheritance: &[InheritanceRelation],
) -> InheritanceGraphItems {
    let mut groups = InheritanceGraphItems::default();
    for relation in inheritance {
        let Some((source_kind, source_id, source_module)) = heritage_endpoint(
            project_id,
            relation.source_kind,
            relation.source_symbol_id.as_deref(),
            &relation.source_name,
            relation.source_external_module.as_deref(),
        ) else {
            continue;
        };
        let Some((target_kind, target_id, target_module)) = heritage_endpoint(
            project_id,
            relation.target_kind,
            relation.target_symbol_id.as_deref(),
            &relation.target_name,
            relation.target_external_module.as_deref(),
        ) else {
            continue;
        };
        groups
            .groups
            .entry((source_kind, target_kind, relation.heritage_kind))
            .or_default()
            .push(HeritageGraphItem {
                source_id,
                target_id,
                source_name: relation.source_name.clone(),
                target_name: relation.target_name.clone(),
                file_path: file_path.to_string(),
                line: relation.line,
                source_module,
                target_module,
            });
    }
    groups
}

fn heritage_endpoint(
    project_id: &str,
    kind: CallTargetKind,
    symbol_id: Option<&str>,
    name: &str,
    module: Option<&str>,
) -> Option<(HeritageEndpoint, String, Option<String>)> {
    match kind {
        CallTargetKind::Symbol => {
            let id = symbol_id.filter(|id| !id.is_empty())?;
            Some((HeritageEndpoint::Symbol, id.to_string(), None))
        }
        CallTargetKind::External => {
            if name.is_empty() {
                return None;
            }
            Some((
                HeritageEndpoint::External,
                make_external_symbol_id(project_id, name, module),
                Some(module.unwrap_or_default().to_string()),
            ))
        }
        CallTargetKind::LocalImport | CallTargetKind::Unresolved => {
            if name.is_empty() {
                return None;
            }
            Some((
                HeritageEndpoint::Unresolved,
                make_unresolved_callee_id(project_id, name),
                None,
            ))
        }
    }
}

pub(super) fn add_inheritance_query(
    project_id: &str,
    source: HeritageEndpoint,
    target: HeritageEndpoint,
    kind: HeritageKind,
    rows: &[HeritageGraphItem],
    content_hash: &str,
    sync_token: &str,
) -> anyhow::Result<TypedQuery> {
    let mut params = vec![
        ("project", TypedValue::String(project_id.to_string())),
        ("rows", heritage_rows(rows)?),
    ];
    params.extend(metadata_params(sync_token, content_hash));
    typed_query(heritage_cypher(kind, source, target), params)
}

fn heritage_rows(rows: &[HeritageGraphItem]) -> anyhow::Result<TypedValue> {
    Ok(TypedValue::List(
        rows.iter()
            .map(|row| {
                Ok(map_value([
                    ("source_id", TypedValue::String(row.source_id.clone())),
                    ("target_id", TypedValue::String(row.target_id.clone())),
                    ("source_name", TypedValue::String(row.source_name.clone())),
                    ("target_name", TypedValue::String(row.target_name.clone())),
                    ("file_path", TypedValue::String(row.file_path.clone())),
                    ("line", usize_value(row.line)?),
                    (
                        "source_module",
                        TypedValue::String(row.source_module.clone().unwrap_or_default()),
                    ),
                    (
                        "target_module",
                        TypedValue::String(row.target_module.clone().unwrap_or_default()),
                    ),
                ]))
            })
            .collect::<anyhow::Result<Vec<_>>>()?,
    ))
}
