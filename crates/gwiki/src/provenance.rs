//! Provenance links from raw source chunks to synthesized wiki sections.

use std::fs;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::WikiError;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceChunkRef {
    pub source_id: String,
    pub chunk_id: String,
    pub path: PathBuf,
    /// Inclusive byte offset in the source note.
    pub byte_start: usize,
    /// Exclusive byte offset in the source note.
    pub byte_end: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WikiSectionRef {
    pub page_path: PathBuf,
    pub heading: String,
    pub section_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProvenanceLink {
    pub source: SourceChunkRef,
    pub section: WikiSectionRef,
    pub claim: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProvenanceGraph {
    links: Vec<ProvenanceLink>,
}

impl ProvenanceGraph {
    pub fn add_link(&mut self, link: ProvenanceLink) {
        self.links.push(link);
    }

    pub fn links_for_section(&self, section_id: &str) -> Vec<&ProvenanceLink> {
        self.links
            .iter()
            .filter(|link| link.section.section_id == section_id)
            .collect()
    }

    pub fn links_for_source(&self, source_id: &str) -> Vec<&ProvenanceLink> {
        self.links
            .iter()
            .filter(|link| link.source.source_id == source_id)
            .collect()
    }

    pub fn links(&self) -> &[ProvenanceLink] {
        &self.links
    }

    pub fn save_to_vault(&self, vault_root: &std::path::Path) -> Result<(), WikiError> {
        let meta_dir = vault_root.join("meta");
        fs::create_dir_all(&meta_dir).map_err(|error| WikiError::Io {
            action: "create provenance metadata directory",
            path: Some(meta_dir.clone()),
            source: error,
        })?;
        let path = meta_dir.join("provenance.json");
        let json = serde_json::to_string_pretty(self).map_err(|error| WikiError::Json {
            action: "serialize provenance graph",
            path: Some(path.clone()),
            source: error,
        })?;
        fs::write(&path, json).map_err(|error| WikiError::Io {
            action: "write provenance graph",
            path: Some(path),
            source: error,
        })
    }

    pub fn load_from_vault(vault_root: &std::path::Path) -> Result<Self, WikiError> {
        let path = vault_root.join("meta").join("provenance.json");
        let json = fs::read_to_string(&path).map_err(|error| WikiError::Io {
            action: "read provenance graph",
            path: Some(path.clone()),
            source: error,
        })?;
        serde_json::from_str(&json).map_err(|error| WikiError::Json {
            action: "parse provenance graph",
            path: Some(path),
            source: error,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn links_sources_to_sections() {
        let source = SourceChunkRef {
            source_id: "src-provenance".to_string(),
            chunk_id: "chunk-42".to_string(),
            path: PathBuf::from("raw/research/provenance.md"),
            byte_start: 12,
            byte_end: 96,
        };
        let section = WikiSectionRef {
            page_path: PathBuf::from("wiki/topics/provenance.md"),
            heading: "Durable provenance".to_string(),
            section_id: "durable-provenance".to_string(),
        };
        let mut graph = ProvenanceGraph::default();

        graph.add_link(ProvenanceLink {
            source: source.clone(),
            section: section.clone(),
            claim: Some("Raw chunks support synthesized sections.".to_string()),
        });

        let section_links = graph.links_for_section("durable-provenance");
        assert_eq!(section_links.len(), 1);
        assert_eq!(section_links[0].source, source);
        assert_eq!(section_links[0].section, section);
        assert_eq!(
            graph.links_for_source("src-provenance")[0]
                .section
                .section_id,
            "durable-provenance"
        );
    }

    #[test]
    fn saves_and_loads_vault_roundtrip() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut graph = ProvenanceGraph::default();
        graph.add_link(ProvenanceLink {
            source: SourceChunkRef {
                source_id: "src-roundtrip".to_string(),
                chunk_id: "chunk-1".to_string(),
                path: PathBuf::from("raw/source.md"),
                byte_start: 0,
                byte_end: 42,
            },
            section: WikiSectionRef {
                page_path: PathBuf::from("wiki/topics/roundtrip.md"),
                heading: "Roundtrip".to_string(),
                section_id: "roundtrip".to_string(),
            },
            claim: Some("Persistence preserves provenance links.".to_string()),
        });

        graph.save_to_vault(temp.path()).expect("save provenance");
        let loaded = ProvenanceGraph::load_from_vault(temp.path()).expect("load provenance");

        assert_eq!(loaded, graph);
    }
}
