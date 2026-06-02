//! Provenance links from raw source chunks to synthesized wiki sections.

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
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
    #[serde(default, skip)]
    section_index: BTreeMap<String, Vec<usize>>,
    #[serde(default, skip)]
    source_index: BTreeMap<String, Vec<usize>>,
}

impl ProvenanceGraph {
    pub fn add_link(&mut self, link: ProvenanceLink) {
        let index = self.links.len();
        self.section_index
            .entry(link.section.section_id.clone())
            .or_default()
            .push(index);
        self.source_index
            .entry(link.source.source_id.clone())
            .or_default()
            .push(index);
        self.links.push(link);
    }

    pub fn links_for_section(&self, section_id: &str) -> Vec<&ProvenanceLink> {
        self.section_index
            .get(section_id)
            .into_iter()
            .flatten()
            .filter_map(|index| self.links.get(*index))
            .collect()
    }

    pub fn links_for_source(&self, source_id: &str) -> Vec<&ProvenanceLink> {
        self.source_index
            .get(source_id)
            .into_iter()
            .flatten()
            .filter_map(|index| self.links.get(*index))
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
        write_provenance_json_durably(&meta_dir, &path, json.as_bytes())
    }

    pub fn load_from_vault(vault_root: &std::path::Path) -> Result<Self, WikiError> {
        let path = vault_root.join("meta").join("provenance.json");
        let json = match fs::read_to_string(&path) {
            Ok(json) => json,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self::default());
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "read provenance graph",
                    path: Some(path.clone()),
                    source: error,
                });
            }
        };
        let mut graph: Self = serde_json::from_str(&json).map_err(|error| WikiError::Json {
            action: "parse provenance graph",
            path: Some(path.clone()),
            source: error,
        })?;
        graph.rebuild_indexes();
        Ok(graph)
    }

    fn rebuild_indexes(&mut self) {
        self.section_index.clear();
        self.source_index.clear();
        for (index, link) in self.links.iter().enumerate() {
            self.section_index
                .entry(link.section.section_id.clone())
                .or_default()
                .push(index);
            self.source_index
                .entry(link.source.source_id.clone())
                .or_default()
                .push(index);
        }
    }
}

fn write_provenance_json_durably(
    meta_dir: &std::path::Path,
    path: &std::path::Path,
    contents: &[u8],
) -> Result<(), WikiError> {
    let temp_path = meta_dir.join(format!(".provenance.json.{}.tmp", std::process::id()));
    let mut temp = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temp_path)
        .map_err(|source| WikiError::Io {
            action: "create provenance graph temp file",
            path: Some(temp_path.clone()),
            source,
        })?;
    if let Err(source) = temp.write_all(contents) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "write provenance graph temp file",
            path: Some(temp_path),
            source,
        });
    }
    if let Err(source) = temp.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync provenance graph temp file",
            path: Some(temp_path),
            source,
        });
    }
    drop(temp);
    fs::rename(&temp_path, path).map_err(|source| WikiError::Io {
        action: "replace provenance graph",
        path: Some(path.to_path_buf()),
        source,
    })?;
    sync_provenance_dir(meta_dir)
}

fn sync_provenance_dir(meta_dir: &std::path::Path) -> Result<(), WikiError> {
    #[cfg(not(unix))]
    {
        let _ = meta_dir;
        Ok(())
    }
    #[cfg(unix)]
    {
        fs::File::open(meta_dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|source| WikiError::Io {
                action: "sync provenance metadata directory",
                path: Some(meta_dir.to_path_buf()),
                source,
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

    #[test]
    fn missing_provenance_file_loads_empty_graph() {
        let temp = tempfile::tempdir().expect("tempdir");

        let graph = ProvenanceGraph::load_from_vault(temp.path()).expect("missing graph");

        assert!(graph.links().is_empty());
    }
}
