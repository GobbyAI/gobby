use super::*;

/// Visual shape for an evidence node, applied deterministically at
/// normalization so the model cannot unbalance a bracket pair.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) enum NodeShape {
    /// `id["label"]` — the default component box.
    #[default]
    Box,
    /// `id(["label"])` — runnable entry points (binaries).
    Stadium,
    /// `id[("label")]` — service/datastore boundaries.
    Cylinder,
}

/// One node the model is allowed to draw.
#[derive(Clone, Debug)]
pub(crate) struct EvidenceNode {
    /// Stable Mermaid identifier the model must reference verbatim.
    pub(crate) id: String,
    /// Human label, escaped at normalization (never trusted from the model).
    pub(crate) label: String,
    pub(crate) shape: NodeShape,
}

/// One directed edge the model is allowed to draw. Style and label are
/// canonical: the normalizer re-attaches them from the evidence regardless of
/// how the model wrote the arrow.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct EvidenceEdge {
    pub(crate) from: String,
    pub(crate) to: String,
    /// Optional arrow label (e.g. a service dependency strength).
    pub(crate) label: Option<String>,
    /// Dotted (`-.->`) versus solid (`-->`) arrow.
    pub(crate) dotted: bool,
}

/// The full evidence graph supplied to one composition call.
#[derive(Clone, Debug, Default)]
pub(crate) struct DiagramEvidence {
    pub(crate) nodes: Vec<EvidenceNode>,
    pub(crate) edges: Vec<EvidenceEdge>,
}

impl DiagramEvidence {
    /// True when there is nothing evidenced to draw: fewer than two nodes or
    /// no edges. Callers emit no diagram in that case (normal, not degraded).
    pub(crate) fn is_sparse(&self) -> bool {
        self.nodes.len() < 2 || self.edges.is_empty()
    }

    pub(crate) fn push_node(
        &mut self,
        id: impl Into<String>,
        label: impl Into<String>,
        shape: NodeShape,
    ) {
        let id = id.into();
        if self.nodes.iter().all(|node| node.id != id) {
            self.nodes.push(EvidenceNode {
                id,
                label: label.into(),
                shape,
            });
        }
    }

    pub(crate) fn push_edge(
        &mut self,
        from: impl Into<String>,
        to: impl Into<String>,
        label: Option<String>,
        dotted: bool,
    ) {
        let from = from.into();
        let to = to.into();
        debug_assert!(
            self.node(&from).is_some(),
            "diagram evidence edge references missing source node `{from}`"
        );
        debug_assert!(
            self.node(&to).is_some(),
            "diagram evidence edge references missing target node `{to}`"
        );
        let edge = EvidenceEdge {
            from,
            to,
            label,
            dotted,
        };
        if edge.from != edge.to && !self.edges.contains(&edge) {
            self.edges.push(edge);
        }
    }

    pub(super) fn node(&self, id: &str) -> Option<&EvidenceNode> {
        self.nodes.iter().find(|node| node.id == id)
    }

    pub(super) fn edge(&self, from: &str, to: &str) -> Option<&EvidenceEdge> {
        self.edges
            .iter()
            .find(|edge| edge.from == from && edge.to == to)
    }

    /// Render the evidence block of the composition prompt: the only nodes and
    /// arrows the model may draw.
    pub(super) fn prompt_block(&self) -> String {
        let mut block = String::from("Nodes (id: label):\n");
        for node in &self.nodes {
            let _ = writeln!(block, "- {}: {}", node.id, node.label);
        }
        block.push_str("\nEvidence edges (the only arrows you may draw):\n");
        for edge in &self.edges {
            match &edge.label {
                Some(label) => {
                    let _ = writeln!(block, "- {} -> {} ({label})", edge.from, edge.to);
                }
                None => {
                    let _ = writeln!(block, "- {} -> {}", edge.from, edge.to);
                }
            }
        }
        block
    }
}
