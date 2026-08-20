//! JSON + Mermaid payload for `gcode graph view`.

use anyhow::{Context as _, bail};
use gobby_core::graph_analytics::{AnalyticsEdge, AnalyticsGraph, AnalyticsNode, weight_for_kind};
use gobby_core::vault::mermaid::{escape_label, is_valid_mermaid};
use serde::Serialize;

use crate::cli::GraphViewKind;
use crate::output::{self, Format};

#[allow(dead_code)]
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub(super) enum NodeKind {
    Symbol,
    File,
    Module,
    External,
    Unresolved,
}

impl NodeKind {
    fn key_prefix(self) -> &'static str {
        match self {
            Self::Symbol => "symbol",
            Self::File => "file",
            Self::Module => "module",
            Self::External => "external",
            Self::Unresolved => "unresolved",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub(super) struct NodeKey {
    pub kind: NodeKind,
    pub identity: String,
}

impl NodeKey {
    #[allow(dead_code)]
    pub(super) fn symbol(id: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::Symbol,
            identity: id.into(),
        }
    }

    #[allow(dead_code)]
    pub(super) fn file(path: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::File,
            identity: path.into(),
        }
    }

    #[allow(dead_code)]
    pub(super) fn module(name: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::Module,
            identity: name.into(),
        }
    }

    #[allow(dead_code)]
    pub(super) fn external(id: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::External,
            identity: id.into(),
        }
    }

    #[allow(dead_code)]
    pub(super) fn unresolved(id: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::Unresolved,
            identity: id.into(),
        }
    }

    pub(super) fn canonical(&self) -> String {
        format!("{}:{}", self.kind.key_prefix(), self.identity)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(super) struct ViewSeed {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub file: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(super) struct ViewNode {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub file: Option<String>,
    pub community: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(super) struct ViewEdge {
    pub source: String,
    pub target: String,
    pub rel: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(super) struct ViewCommunity {
    pub id: String,
    pub nodes: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(super) struct ViewPayload {
    pub project_id: String,
    pub project_root: String,
    pub view: String,
    pub seed: ViewSeed,
    pub depth: u32,
    pub incoming_truncated: bool,
    pub outgoing_truncated: bool,
    pub hint: Option<String>,
    pub nodes: Vec<ViewNode>,
    pub edges: Vec<ViewEdge>,
    pub communities: Vec<ViewCommunity>,
    pub mermaid: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct ViewNodeInput {
    pub key: NodeKey,
    pub name: String,
    pub kind: String,
    pub file: Option<String>,
    pub community: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct ViewEdgeInput {
    pub source: NodeKey,
    pub target: NodeKey,
    pub rel: String,
}

#[allow(dead_code)]
pub(super) fn node_file_for_kind(
    kind: NodeKind,
    declaring_file: Option<String>,
    module_providers: &[String],
) -> Option<String> {
    match kind {
        NodeKind::File | NodeKind::Symbol => declaring_file,
        NodeKind::Module => {
            if module_providers.len() == 1 {
                Some(module_providers[0].clone())
            } else {
                None
            }
        }
        NodeKind::External | NodeKind::Unresolved => None,
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn build_view_payload(
    project_id: impl Into<String>,
    project_root: impl Into<String>,
    view: GraphViewKind,
    seed: ViewSeed,
    depth: u32,
    incoming_truncated: bool,
    outgoing_truncated: bool,
    hint: Option<String>,
    nodes: Vec<ViewNodeInput>,
    edges: Vec<ViewEdgeInput>,
    communities: Vec<ViewCommunity>,
) -> anyhow::Result<ViewPayload> {
    let mut nodes = nodes;
    let mut edges = edges;
    nodes.sort_by_key(|left| left.key.canonical());
    nodes.dedup_by(|left, right| left.key == right.key);
    edges.sort_by(|left, right| {
        left.source
            .canonical()
            .cmp(&right.source.canonical())
            .then_with(|| left.target.canonical().cmp(&right.target.canonical()))
            .then_with(|| left.rel.cmp(&right.rel))
    });
    edges.dedup_by(|left, right| {
        left.source == right.source && left.target == right.target && left.rel == right.rel
    });

    let rendered_nodes = nodes
        .iter()
        .map(|node| ViewNode {
            id: node.key.canonical(),
            name: node.name.clone(),
            kind: node.kind.clone(),
            file: node.file.clone(),
            community: node.community.clone(),
        })
        .collect::<Vec<_>>();
    let rendered_edges = edges
        .iter()
        .map(|edge| ViewEdge {
            source: edge.source.canonical(),
            target: edge.target.canonical(),
            rel: edge.rel.clone(),
        })
        .collect::<Vec<_>>();
    let mermaid = render_mermaid(&seed, &rendered_nodes, &rendered_edges)?;
    Ok(ViewPayload {
        project_id: project_id.into(),
        project_root: project_root.into(),
        view: view.as_str().to_string(),
        seed,
        depth,
        incoming_truncated,
        outgoing_truncated,
        hint,
        nodes: rendered_nodes,
        edges: rendered_edges,
        communities,
        mermaid,
    })
}

#[allow(dead_code)]
pub(super) fn analytics_graph_from_payload(payload: &ViewPayload) -> AnalyticsGraph {
    AnalyticsGraph {
        nodes: payload
            .nodes
            .iter()
            .map(|node| AnalyticsNode {
                id: node.id.clone(),
                kind: node.kind.clone(),
                weight: 1.0,
            })
            .collect(),
        edges: payload
            .edges
            .iter()
            .map(|edge| AnalyticsEdge {
                source: edge.source.clone(),
                target: edge.target.clone(),
                kind: edge.rel.clone(),
                weight: weight_for_kind(&edge.rel),
            })
            .collect(),
    }
}

pub(super) fn format_view_output(payload: &ViewPayload) -> anyhow::Result<String> {
    ensure_valid_mermaid(&payload.mermaid)?;
    serde_json::to_string_pretty(payload).context("serialize graph view payload")
}

pub(super) fn print_view(payload: &ViewPayload, format: Format) -> anyhow::Result<()> {
    let json = format_view_output(payload)?;
    match format {
        Format::Json => output::print_text(&json),
        Format::Text => {
            println!("{json}");
            println!();
            println!("{}", payload.mermaid);
            Ok(())
        }
    }
}

fn ensure_valid_mermaid(block: &str) -> anyhow::Result<()> {
    if is_valid_mermaid(block) {
        Ok(())
    } else {
        bail!("generated mermaid failed validation")
    }
}

fn render_mermaid(
    seed: &ViewSeed,
    nodes: &[ViewNode],
    edges: &[ViewEdge],
) -> anyhow::Result<String> {
    let mut keys = nodes.iter().map(|node| node.id.clone()).collect::<Vec<_>>();
    if keys.is_empty() {
        keys.push(format!("symbol:{}", seed.id));
    }
    keys.sort();
    keys.dedup();
    let mermaid_ids = keys
        .iter()
        .enumerate()
        .map(|(index, _)| format!("n{index}"))
        .collect::<Vec<_>>();
    let mut id_for = std::collections::BTreeMap::new();
    for (key, mermaid_id) in keys.iter().zip(mermaid_ids.iter()) {
        id_for.insert(key.clone(), mermaid_id.clone());
    }

    let mut lines = vec!["```mermaid".to_string(), "flowchart TB".to_string()];
    if nodes.is_empty() {
        let token = id_for
            .get(&format!("symbol:{}", seed.id))
            .cloned()
            .unwrap_or_else(|| "n0".to_string());
        lines.push(format!("    {token}[\"{}\"]", mermaid_label(&seed.name)));
    } else {
        for node in nodes {
            let token = id_for
                .get(&node.id)
                .cloned()
                .with_context(|| format!("missing mermaid token for {}", node.id))?;
            let label = match &node.community {
                Some(community) if !community.is_empty() => {
                    format!("{} [{community}]", node.name)
                }
                _ => node.name.clone(),
            };
            lines.push(format!("    {token}[\"{}\"]", mermaid_label(&label)));
        }
    }
    for edge in edges {
        let source = id_for
            .get(&edge.source)
            .cloned()
            .with_context(|| format!("missing mermaid token for {}", edge.source))?;
        let target = id_for
            .get(&edge.target)
            .cloned()
            .with_context(|| format!("missing mermaid token for {}", edge.target))?;
        lines.push(format!(
            "    {source} -->|\"{}\"| {target}",
            mermaid_label(&edge.rel)
        ));
    }
    lines.push("```".to_string());
    let block = lines.join("\n");
    ensure_valid_mermaid(&block)?;
    Ok(block)
}

fn mermaid_label(text: &str) -> String {
    escape_label(&text.replace(['\n', '\r'], " "))
}

#[cfg(test)]
#[path = "render_tests.rs"]
mod tests;
