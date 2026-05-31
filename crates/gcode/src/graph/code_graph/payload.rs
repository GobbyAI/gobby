use std::collections::HashSet;

use serde::{Deserialize, Serialize};

use crate::models::{ProjectionMetadata, ProjectionProvenance};
use gobby_core::falkor::Row;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct GraphPayload {
    pub nodes: Vec<GraphNode>,
    pub links: Vec<GraphLink>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub center: Option<String>,
    #[serde(skip)]
    node_ids: HashSet<String>,
}

impl GraphPayload {
    pub fn with_center(center: impl Into<String>) -> Self {
        Self {
            nodes: vec![],
            links: vec![],
            center: Some(center.into()),
            node_ids: HashSet::new(),
        }
    }

    pub fn push_node(&mut self, node: GraphNode) {
        if node.id.is_empty() {
            return;
        }
        self.refresh_node_cache_if_needed();
        if !self.node_ids.insert(node.id.clone()) {
            return;
        }
        self.nodes.push(node);
    }

    fn refresh_node_cache_if_needed(&mut self) {
        if self.node_ids.len() == self.nodes.len() {
            return;
        }
        self.node_ids = self
            .nodes
            .iter()
            .filter(|node| !node.id.is_empty())
            .map(|node| node.id.clone())
            .collect();
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GraphNode {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub node_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub file_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub symbol_count: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub blast_distance: Option<usize>,
}

impl GraphNode {
    pub fn new(
        id: impl Into<String>,
        name: impl Into<String>,
        node_type: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            name: name.into(),
            node_type: node_type.into(),
            kind: None,
            file_path: None,
            line_start: None,
            signature: None,
            symbol_count: None,
            language: None,
            blast_distance: None,
        }
    }

    /// Builds a node from unprefixed Falkor rows.
    ///
    /// Fallback key priority is id: `id`, `node_id`; name: `name`,
    /// `node_name`, then id; type: `type`, `node_type`, then `default_type`.
    pub(super) fn from_row(row: &Row, default_type: &str) -> Option<Self> {
        let id = row_string_owned(row, &["id", "node_id"])?;
        let mut node = Self::new(
            id.clone(),
            row_string_owned(row, &["name", "node_name"]).unwrap_or(id),
            row_string_owned(row, &["type", "node_type"])
                .unwrap_or_else(|| default_type.to_string()),
        );
        node.kind = row_string_owned(row, &["kind"]);
        node.file_path = row_string_owned(row, &["file_path"]);
        node.line_start = row_usize(row, &["line_start", "line"]);
        node.signature = row_string_owned(row, &["signature"]);
        node.symbol_count = row_usize(row, &["symbol_count"]);
        node.language = row_string_owned(row, &["language"]);
        node.blast_distance = row_usize(row, &["blast_distance", "distance"]);
        Some(node)
    }

    pub(super) fn from_prefixed_row(row: &Row, prefix: &str, default_type: &str) -> Option<Self> {
        let id_key = format!("{prefix}_id");
        let name_key = format!("{prefix}_name");
        let type_key = format!("{prefix}_type");
        let kind_key = format!("{prefix}_kind");
        let file_path_key = format!("{prefix}_file_path");
        let line_start_key = format!("{prefix}_line_start");
        let signature_key = format!("{prefix}_signature");

        let id = row_string_owned(row, &[id_key.as_str()])?;
        let mut node = Self::new(
            id.clone(),
            row_string_owned(row, &[name_key.as_str()]).unwrap_or(id),
            row_string_owned(row, &[type_key.as_str()]).unwrap_or_else(|| default_type.to_string()),
        );
        node.kind = row_string_owned(row, &[kind_key.as_str()]);
        node.file_path = row_string_owned(row, &[file_path_key.as_str()]);
        node.line_start = row_usize_owned(row, &[line_start_key.as_str()]);
        node.signature = row_string_owned(row, &[signature_key.as_str()]);
        Some(node)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GraphLink {
    pub source: String,
    pub target: String,
    #[serde(rename = "type")]
    pub link_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub distance: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<ProjectionMetadata>,
}

impl GraphLink {
    pub fn new(
        source: impl Into<String>,
        target: impl Into<String>,
        link_type: impl Into<String>,
    ) -> Self {
        Self {
            source: source.into(),
            target: target.into(),
            link_type: link_type.into(),
            line: None,
            distance: None,
            metadata: None,
        }
    }

    pub fn from_row(row: &Row) -> Option<Self> {
        let mut link = Self::new(
            row_string_owned(row, &["source"])?,
            row_string_owned(row, &["target"])?,
            row_string_owned(row, &["type", "rel_type"]).unwrap_or_else(|| "CALLS".to_string()),
        );
        link.line = row_usize(row, &["line"]);
        link.distance = row_usize(row, &["distance"]);
        link.metadata = row_to_projection_metadata(row);
        Some(link)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphBlastRadiusTarget {
    SymbolId(String),
    FilePath(String),
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

pub(super) fn row_to_projection_metadata(row: &Row) -> Option<ProjectionMetadata> {
    let provenance = row
        .get("provenance")
        .and_then(|v| v.as_str())
        .and_then(ProjectionProvenance::from_wire_value)?;
    let source_system = row.get("source_system").and_then(|v| v.as_str())?;

    let mut metadata = ProjectionMetadata::new(provenance, source_system);
    metadata.confidence = row.get("confidence").and_then(|v| v.as_f64());
    metadata.source_file_path = row_string_owned(row, &["metadata_source_file_path"]);
    metadata.source_line = row
        .get("source_line")
        .or_else(|| row.get("line"))
        .and_then(|v| v.as_u64())
        .and_then(|line| usize::try_from(line).ok());
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

pub(super) fn row_string_owned(row: &Row, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| row.get(*key).and_then(|value| value.as_str()))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

pub(super) fn row_usize(row: &Row, keys: &[&str]) -> Option<usize> {
    row_usize_owned(row, keys)
}

fn row_usize_owned(row: &Row, keys: &[&str]) -> Option<usize> {
    for key in keys {
        let Some(value) = row.get(*key) else {
            continue;
        };
        if let Some(value) = value.as_u64() {
            return usize::try_from(value).ok();
        }
        if let Some(value) = value.as_i64() {
            if let Ok(value) = usize::try_from(value) {
                return Some(value);
            }
            log::warn!("negative graph payload integer ignored; key={key} value={value}");
        }
    }
    None
}

pub(super) fn add_link_from_row(payload: &mut GraphPayload, row: &Row) {
    if let Some(link) = GraphLink::from_row(row) {
        payload.links.push(link);
    }
}

pub(super) fn add_node_from_row(payload: &mut GraphPayload, row: &Row, default_type: &str) {
    if let Some(node) = GraphNode::from_row(row, default_type) {
        payload.push_node(node);
    }
}

pub(super) fn add_prefixed_node_from_row(
    payload: &mut GraphPayload,
    row: &Row,
    prefix: &str,
    default_type: &str,
) {
    if let Some(node) = GraphNode::from_prefixed_row(row, prefix, default_type) {
        payload.push_node(node);
    }
}
