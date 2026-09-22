//! JSON + Mermaid payload for `gcode graph view`.

use anyhow::{Context as _, bail};
use gobby_core::mermaid::{escape_label, is_valid_mermaid};
use serde::Serialize;

use crate::cli::GraphViewKind;
use crate::output::{self, Format};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub(super) enum NodeKind {
    Symbol,
    File,
    Module,
    Community,
    External,
    Unresolved,
}

impl NodeKind {
    pub(super) fn key_prefix(self) -> &'static str {
        match self {
            Self::Symbol => "symbol",
            Self::File => "file",
            Self::Module => "module",
            Self::Community => "community",
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
    pub(super) fn symbol(id: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::Symbol,
            identity: id.into(),
        }
    }

    pub(super) fn file(path: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::File,
            identity: path.into(),
        }
    }

    pub(super) fn module(name: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::Module,
            identity: name.into(),
        }
    }

    pub(super) fn community(id: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::Community,
            identity: id.into(),
        }
    }

    pub(super) fn external(id: impl Into<String>) -> Self {
        Self {
            kind: NodeKind::External,
            identity: id.into(),
        }
    }

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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub count: Option<usize>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub(super) struct ViewCommunity {
    pub id: String,
    pub label: String,
    pub size: usize,
    pub cohesion: f64,
    pub label_source: String,
    pub label_stale: bool,
    /// Canonical node ids that are present in this view; `size` remains project-wide.
    pub nodes: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
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
        NodeKind::Community | NodeKind::External | NodeKind::Unresolved => None,
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
    let mut communities = communities;
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
            count: None,
        })
        .collect::<Vec<_>>();
    let node_by_id = rendered_nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<std::collections::HashMap<_, _>>();
    for community in &mut communities {
        community.nodes.sort();
        community.nodes.dedup();
        for member in &community.nodes {
            let Some(node) = node_by_id.get(member.as_str()) else {
                bail!(
                    "community {} contains unknown view node {member}",
                    community.id
                );
            };
            if node.community.as_deref() != Some(community.id.as_str()) {
                bail!(
                    "community {} contains mismatched view node {member}",
                    community.id
                );
            }
        }
    }
    communities.sort_by(|left, right| left.id.cmp(&right.id));
    let members_by_community = communities
        .iter()
        .map(|community| {
            (
                community.id.as_str(),
                community
                    .nodes
                    .iter()
                    .map(String::as_str)
                    .collect::<std::collections::HashSet<_>>(),
            )
        })
        .collect::<std::collections::HashMap<_, _>>();
    for node in &rendered_nodes {
        if let Some(community_id) = node.community.as_deref() {
            let Some(members) = members_by_community.get(community_id) else {
                bail!(
                    "view node {} names unlisted community {community_id}",
                    node.id
                );
            };
            if !members.contains(node.id.as_str()) {
                bail!(
                    "community {community_id} does not include view node {}",
                    node.id
                );
            }
        }
    }
    let mermaid = render_mermaid(&seed, &rendered_nodes, &rendered_edges, &communities)?;
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
    communities: &[ViewCommunity],
) -> anyhow::Result<String> {
    let mut keys = nodes.iter().map(|node| node.id.clone()).collect::<Vec<_>>();
    keys.sort();
    keys.dedup();
    let mut id_for = std::collections::BTreeMap::new();
    for (index, key) in keys.into_iter().enumerate() {
        id_for.insert(key, format!("n{index}"));
    }

    let mut lines = vec!["```mermaid".to_string(), "flowchart TB".to_string()];
    if nodes.is_empty() {
        lines.push(format!("    n0[\"{}\"]", mermaid_label(&seed.name)));
    } else {
        let node_by_id = nodes
            .iter()
            .map(|node| (node.id.as_str(), node))
            .collect::<std::collections::HashMap<_, _>>();
        let clustered = communities
            .iter()
            .flat_map(|community| community.nodes.iter().map(String::as_str))
            .collect::<std::collections::HashSet<_>>();
        let render_node = |node: &ViewNode, indent: &str| -> anyhow::Result<String> {
            let token = id_for
                .get(&node.id)
                .cloned()
                .with_context(|| format!("missing mermaid token for {}", node.id))?;
            Ok(format!(
                "{indent}{token}[\"{}\"]",
                mermaid_label(&node.name)
            ))
        };
        for community in communities {
            if community.nodes.is_empty() {
                continue;
            }
            let community_id = community
                .id
                .strip_prefix("community:")
                .and_then(|id| id.parse::<u32>().ok())
                .with_context(|| format!("invalid community id {}", community.id))?;
            lines.push(format!(
                "    subgraph community_{community_id}[\"{}\"]",
                mermaid_label(&community.label)
            ));
            for node_id in &community.nodes {
                let node = node_by_id
                    .get(node_id.as_str())
                    .with_context(|| format!("missing community node {node_id}"))?;
                lines.push(render_node(node, "        ")?);
            }
            lines.push("    end".to_string());
        }
        for node in nodes {
            if !clustered.contains(node.id.as_str()) {
                lines.push(render_node(node, "    ")?);
            }
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
        let label = match edge.count {
            Some(count) => format!("{} ({count})", edge.rel),
            None => edge.rel.clone(),
        };
        lines.push(format!(
            "    {source} -->|\"{}\"| {target}",
            mermaid_label(&label)
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
