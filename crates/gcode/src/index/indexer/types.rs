use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::projection::sync::{ProjectionSyncStatus, ProjectionTarget};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexRequest {
    pub project_root: PathBuf,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path_filter: Option<PathBuf>,
    #[serde(default)]
    pub explicit_files: Vec<PathBuf>,
    pub full: bool,
    pub require_cpp_semantics: bool,
    pub sync_projections: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexDurations {
    pub discovery_ms: u64,
    pub indexing_ms: u64,
    pub stats_ms: u64,
    pub total_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum IndexDegradation {
    FileIndexError {
        file_path: String,
        message: String,
    },
    ProjectionSyncSkipped {
        reason: String,
    },
    ProjectionCleanupFailed {
        file_path: String,
        target: ProjectionTarget,
        message: String,
    },
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexOutcome {
    pub project_id: String,
    pub scanned_files: usize,
    pub indexed_files: usize,
    pub skipped_files: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub unsupported_file_types: Vec<UnsupportedFileType>,
    pub symbols_indexed: usize,
    pub imports_indexed: usize,
    pub calls_indexed: usize,
    pub unresolved_targets_indexed: usize,
    pub chunks_indexed: usize,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub tombstones_indexed: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub indexed_file_paths: Vec<String>,
    pub durations: IndexDurations,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub degraded: Vec<IndexDegradation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub projection_sync: Option<ProjectionSyncStatus>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub overlay: Option<OverlayIndexMetadata>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnsupportedFileType {
    pub extension: String,
    pub files: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub examples: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OverlayIndexMetadata {
    pub overlay_project_id: String,
    pub overlay_root: String,
    pub parent_project_id: String,
    pub parent_root: String,
}

impl IndexOutcome {
    pub(super) fn new(project_id: &str) -> Self {
        Self {
            project_id: project_id.to_string(),
            ..Self::default()
        }
    }

    pub(super) fn add_counts(&mut self, counts: FileIndexCounts) {
        self.indexed_files += counts.indexed_files;
        self.symbols_indexed += counts.symbols_indexed;
        self.imports_indexed += counts.imports_indexed;
        self.calls_indexed += counts.calls_indexed;
        self.unresolved_targets_indexed += counts.unresolved_targets_indexed;
        self.chunks_indexed += counts.chunks_indexed;
        if counts.indexed_files > 0 {
            self.indexed_file_paths.push(counts.file_path);
        }
    }

    pub(super) fn set_unsupported_file_types(&mut self, unsupported: Vec<UnsupportedFileType>) {
        self.unsupported_file_types = unsupported;
    }
}

fn is_zero(value: &usize) -> bool {
    *value == 0
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(super) struct FileIndexCounts {
    pub(super) file_path: String,
    pub(super) indexed_files: usize,
    pub(super) symbols_indexed: usize,
    pub(super) imports_indexed: usize,
    pub(super) calls_indexed: usize,
    pub(super) unresolved_targets_indexed: usize,
    pub(super) chunks_indexed: usize,
}
