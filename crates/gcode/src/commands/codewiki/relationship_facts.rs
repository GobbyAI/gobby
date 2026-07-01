//! Resolve code-graph edges into cross-file relationship facts for the
//! narrative prose layer.
//!
//! `#884` removed the mechanical mermaid code-graph diagrams but kept the graph
//! fetch so it can feed narrative *analysis*. This module turns the
//! project-wide [`CodewikiGraphEdge`] set (Call/Import edges keyed by symbol
//! component id) into per-file relationship facts: which external symbols call
//! into a file, which external symbols it calls, and which files it imports.
//!
//! Those facts are threaded into the file prompt so `## How it fits` can name
//! concrete cross-file collaborators with real `file:line` citations, and into
//! the verify prompt so the auditor treats them as authoritative evidence
//! instead of false-flagging a grounded relationship claim.

use std::collections::{HashMap, HashSet};

use crate::models::Symbol;

use super::{CodewikiGraphEdge, CodewikiGraphEdgeKind, SourceSpan};

/// Maximum relations rendered per direction (inbound calls, outbound calls,
/// imports). Keeps the prompt bounded and the prose focused on the strongest
/// collaborators rather than the full edge set.
pub(crate) const MAX_RELATIONS_PER_DIRECTION: usize = 5;

/// One resolved cross-file relationship endpoint: the symbol on the other side
/// of a Call/Import edge, plus the location the narrative should cite.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SymbolRelation {
    /// Qualified name of the symbol on the other end of the edge. For an
    /// import this is the imported file's path (the edge is file-level).
    pub(crate) other_name: String,
    /// Kind of the other endpoint (`function`, `method`, `struct`, …); `import`
    /// for file-level import edges.
    pub(crate) other_kind: String,
    /// The local symbol on this file's side of a call edge, when known
    /// (`None` for file-level import edges).
    pub(crate) local_name: Option<String>,
    /// Citable location of the other endpoint.
    pub(crate) span: SourceSpan,
}

impl SymbolRelation {
    /// The `[file:line]` anchor the narrative must reproduce to keep this
    /// relationship cited after the grounding pass.
    pub(crate) fn citation(&self) -> String {
        self.span.citation()
    }
}

/// Per-file cross-file relationships derived from the code graph.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct RelationshipFacts {
    /// External symbols that call symbols defined in this file.
    pub(crate) inbound_calls: Vec<SymbolRelation>,
    /// External symbols that this file's symbols call.
    pub(crate) outbound_calls: Vec<SymbolRelation>,
    /// Files this file imports (resolved to a representative symbol span).
    pub(crate) imports: Vec<SymbolRelation>,
}

impl RelationshipFacts {
    pub(crate) fn is_empty(&self) -> bool {
        self.inbound_calls.is_empty() && self.outbound_calls.is_empty() && self.imports.is_empty()
    }

    /// Citable spans for every relationship endpoint, so the grounding pass
    /// keeps the cross-file `[file:line]` citations the narrative makes. These
    /// widen the grounding allow-list only; they are deliberately *not* added
    /// to the page's own provenance ([`super::FileDoc::source_spans`]), which
    /// must stay scoped to the file itself for reuse hashing.
    pub(crate) fn endpoint_spans(&self) -> Vec<SourceSpan> {
        self.inbound_calls
            .iter()
            .chain(&self.outbound_calls)
            .chain(&self.imports)
            .map(|relation| relation.span.clone())
            .collect()
    }

    /// The distinct cross-file neighbor files this file's narrative depends on
    /// (#885, Leaf H), excluding `own_file`. Recorded so a caller/callee or
    /// import-target edit invalidates this file's page even though the page's
    /// own provenance — kept deliberately narrow (see [`Self::endpoint_spans`])
    /// — did not change.
    pub(crate) fn neighbor_files(&self, own_file: &str) -> std::collections::BTreeSet<String> {
        self.endpoint_spans()
            .into_iter()
            .map(|span| span.file)
            .filter(|file| file != own_file)
            .collect()
    }
}

/// Resolve the project-wide graph edges into cross-file relationship facts for
/// `file`. `file_symbol_ids` are the component ids of symbols defined in the
/// file; `symbols_by_id` resolves any component id to its symbol (for the
/// remote endpoint's name/kind/location). Only genuinely cross-file edges are
/// kept; results are deduped and bounded to [`MAX_RELATIONS_PER_DIRECTION`].
pub(crate) fn relationship_facts_for_file(
    file: &str,
    file_symbol_ids: &HashSet<&str>,
    symbols_by_id: &HashMap<&str, &Symbol>,
    edges: &[CodewikiGraphEdge],
) -> RelationshipFacts {
    let mut inbound = Vec::new();
    let mut outbound = Vec::new();
    let mut imports = Vec::new();
    for edge in edges {
        let source_local = file_symbol_ids.contains(edge.source_component_id.as_str());
        let target_local = file_symbol_ids.contains(edge.target_component_id.as_str());
        match edge.kind {
            CodewikiGraphEdgeKind::Call => {
                if source_local && !target_local {
                    // This file's symbol calls out to the target.
                    if let Some(relation) = call_relation(
                        symbols_by_id,
                        &edge.target_component_id,
                        Some(&edge.source_component_id),
                        file,
                    ) {
                        outbound.push(relation);
                    }
                } else if target_local && !source_local {
                    // The source symbol calls into this file.
                    if let Some(relation) = call_relation(
                        symbols_by_id,
                        &edge.source_component_id,
                        Some(&edge.target_component_id),
                        file,
                    ) {
                        inbound.push(relation);
                    }
                }
            }
            CodewikiGraphEdgeKind::Import => {
                // Import edges are file-level: source is this file's
                // representative component, target is the imported file's.
                if source_local
                    && !target_local
                    && let Some(relation) =
                        import_relation(symbols_by_id, &edge.target_component_id, file)
                {
                    imports.push(relation);
                }
            }
        }
    }
    RelationshipFacts {
        inbound_calls: bound_relations(inbound),
        outbound_calls: bound_relations(outbound),
        imports: bound_relations(imports),
    }
}

/// Resolve one call edge endpoint into a [`SymbolRelation`]. `other_id` is the
/// component on the far side of the edge; `local_id` is this file's symbol (the
/// caller for an outbound call, the callee for an inbound one). Returns `None`
/// when the far endpoint cannot be resolved or is not genuinely cross-file.
fn call_relation(
    symbols_by_id: &HashMap<&str, &Symbol>,
    other_id: &str,
    local_id: Option<&str>,
    file: &str,
) -> Option<SymbolRelation> {
    let other = symbols_by_id.get(other_id)?;
    if other.file_path == file {
        return None;
    }
    let local_name = local_id
        .and_then(|id| symbols_by_id.get(id))
        .map(|symbol| symbol.qualified_name.clone());
    Some(SymbolRelation {
        other_name: other.qualified_name.clone(),
        other_kind: other.kind.clone(),
        local_name,
        span: SourceSpan::from_symbol(other),
    })
}

/// Resolve one import edge target into a file-level [`SymbolRelation`], naming
/// the imported file and citing its representative symbol's span.
fn import_relation(
    symbols_by_id: &HashMap<&str, &Symbol>,
    target_id: &str,
    file: &str,
) -> Option<SymbolRelation> {
    let target = symbols_by_id.get(target_id)?;
    if target.file_path == file {
        return None;
    }
    Some(SymbolRelation {
        other_name: target.file_path.clone(),
        other_kind: "import".to_string(),
        local_name: None,
        span: SourceSpan::from_symbol(target),
    })
}

/// Deterministically order, dedupe, and cap one direction's relations. A single
/// `(other, local)` pair can appear many times (the same callee hit from
/// several call sites); the prose only needs each collaborator once.
fn bound_relations(mut relations: Vec<SymbolRelation>) -> Vec<SymbolRelation> {
    relations.sort_by(|a, b| {
        a.span
            .cmp(&b.span)
            .then_with(|| a.other_name.cmp(&b.other_name))
            .then_with(|| a.local_name.cmp(&b.local_name))
    });
    relations.dedup_by(|a, b| {
        a.other_name == b.other_name && a.local_name == b.local_name && a.span == b.span
    });
    relations.truncate(MAX_RELATIONS_PER_DIRECTION);
    relations
}

#[cfg(test)]
mod tests {
    use super::*;

    fn symbol(
        id: &str,
        file: &str,
        name: &str,
        kind: &str,
        line_start: usize,
        line_end: usize,
    ) -> Symbol {
        Symbol {
            id: id.to_string(),
            project_id: "project-1".to_string(),
            file_path: file.to_string(),
            name: name.to_string(),
            qualified_name: name.to_string(),
            kind: kind.to_string(),
            language: "rust".to_string(),
            byte_start: 0,
            byte_end: 0,
            line_start,
            line_end,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    fn id_set<'a>(ids: &[&'a str]) -> HashSet<&'a str> {
        ids.iter().copied().collect()
    }

    fn by_id(symbols: &[Symbol]) -> HashMap<&str, &Symbol> {
        symbols.iter().map(|s| (s.id.as_str(), s)).collect()
    }

    #[test]
    fn resolves_inbound_and_outbound_cross_file_calls() {
        let symbols = vec![
            symbol("local", "src/a.rs", "a_fn", "function", 10, 20),
            symbol("caller", "src/b.rs", "b_caller", "function", 5, 8),
            symbol("callee", "src/c.rs", "c_callee", "function", 30, 40),
        ];
        let edges = vec![
            CodewikiGraphEdge::call("caller", "local"), // b.rs calls into a.rs
            CodewikiGraphEdge::call("local", "callee"), // a.rs calls out to c.rs
        ];

        let facts =
            relationship_facts_for_file("src/a.rs", &id_set(&["local"]), &by_id(&symbols), &edges);

        assert_eq!(facts.inbound_calls.len(), 1);
        let inbound = &facts.inbound_calls[0];
        assert_eq!(inbound.other_name, "b_caller");
        assert_eq!(inbound.local_name.as_deref(), Some("a_fn"));
        assert_eq!(inbound.citation(), "[src/b.rs:5-8]");

        assert_eq!(facts.outbound_calls.len(), 1);
        let outbound = &facts.outbound_calls[0];
        assert_eq!(outbound.other_name, "c_callee");
        assert_eq!(outbound.local_name.as_deref(), Some("a_fn"));
        assert_eq!(outbound.citation(), "[src/c.rs:30-40]");
    }

    #[test]
    fn drops_same_file_calls() {
        let symbols = vec![
            symbol("one", "src/a.rs", "one", "function", 1, 4),
            symbol("two", "src/a.rs", "two", "function", 6, 9),
        ];
        // Both endpoints live in src/a.rs, so neither is cross-file.
        let edges = vec![CodewikiGraphEdge::call("one", "two")];

        let facts = relationship_facts_for_file(
            "src/a.rs",
            &id_set(&["one", "two"]),
            &by_id(&symbols),
            &edges,
        );

        assert!(facts.is_empty());
    }

    #[test]
    fn resolves_imports_to_target_file() {
        let symbols = vec![
            symbol("local", "src/a.rs", "a_fn", "function", 1, 4),
            symbol("dep", "src/dep.rs", "dep_fn", "function", 12, 18),
        ];
        let edges = vec![CodewikiGraphEdge::import("local", "dep")];

        let facts =
            relationship_facts_for_file("src/a.rs", &id_set(&["local"]), &by_id(&symbols), &edges);

        assert_eq!(facts.imports.len(), 1);
        let import = &facts.imports[0];
        assert_eq!(import.other_name, "src/dep.rs");
        assert_eq!(import.other_kind, "import");
        assert_eq!(import.citation(), "[src/dep.rs:12-18]");
        // Endpoint spans widen the grounding allow-list.
        assert_eq!(facts.endpoint_spans().len(), 1);
    }

    #[test]
    fn dedupes_and_bounds_relations() {
        let mut symbols = vec![symbol("local", "src/a.rs", "a_fn", "function", 1, 4)];
        // Eight distinct external callees, each hit twice (duplicate edges).
        for index in 0..8 {
            symbols.push(symbol(
                &format!("callee{index}"),
                &format!("src/c{index}.rs"),
                &format!("callee{index}"),
                "function",
                index + 1,
                index + 2,
            ));
        }
        let mut edges = Vec::new();
        for index in 0..8 {
            edges.push(CodewikiGraphEdge::call("local", format!("callee{index}")));
            edges.push(CodewikiGraphEdge::call("local", format!("callee{index}")));
        }

        let facts =
            relationship_facts_for_file("src/a.rs", &id_set(&["local"]), &by_id(&symbols), &edges);

        // Duplicates collapse and the direction is capped.
        assert_eq!(facts.outbound_calls.len(), MAX_RELATIONS_PER_DIRECTION);
        let mut names = facts
            .outbound_calls
            .iter()
            .map(|relation| relation.other_name.clone())
            .collect::<Vec<_>>();
        let total = names.len();
        names.sort();
        names.dedup();
        assert_eq!(total, names.len(), "no duplicate collaborators");
    }
}
