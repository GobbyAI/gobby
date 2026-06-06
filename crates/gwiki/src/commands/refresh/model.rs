use super::*;
use serde::ser::SerializeStruct;

#[derive(Debug)]
pub(crate) struct Selection {
    pub(crate) planned: Vec<RefreshPlan>,
    pub(crate) skipped: Vec<SkippedRefresh>,
    pub(crate) failed: Vec<RefreshFailure>,
}

#[derive(Debug)]
pub(crate) struct ChangedRefresh {
    pub(crate) result: ingest::IngestResult,
    pub(crate) previous_raw_path: PathBuf,
    pub(crate) removed_paths: Vec<PathBuf>,
    pub(crate) degradations: Vec<String>,
}

pub(crate) struct RefreshSinks<'a> {
    pub(crate) refreshed: &'a mut Vec<RefreshedSource>,
    pub(crate) unchanged: &'a mut Vec<RefreshResult>,
    pub(crate) failed: &'a mut Vec<RefreshFailure>,
    pub(crate) degradations: &'a mut Vec<String>,
}

#[derive(Debug)]
pub(crate) struct RefreshRender {
    pub(crate) scope: ScopeIdentity,
    pub(crate) dry_run: bool,
    pub(crate) planned: Vec<RefreshPlan>,
    pub(crate) refreshed: Vec<RefreshedSource>,
    pub(crate) unchanged: Vec<RefreshResult>,
    pub(crate) failed: Vec<RefreshFailure>,
    pub(crate) skipped: Vec<SkippedRefresh>,
    pub(crate) index_status: IndexStatus,
    pub(crate) degradations: Vec<String>,
    pub(crate) explicit: bool,
}

#[derive(Debug, Clone)]
pub(crate) struct RefreshPlan {
    pub(crate) record: SourceRecord,
}

impl RefreshPlan {
    pub(crate) fn from_record(record: &SourceRecord) -> Result<Self, WikiError> {
        let _ = raw_source_path(&record.id)?;
        Ok(Self {
            record: record.clone(),
        })
    }
}

impl serde::Serialize for RefreshPlan {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        let raw_path = raw_source_path(&self.record.id).map_err(serde::ser::Error::custom)?;
        let mut state = serializer.serialize_struct("RefreshPlan", 6)?;
        state.serialize_field("id", &self.record.id)?;
        state.serialize_field("location", &self.record.location)?;
        state.serialize_field("source_kind", &self.record.kind)?;
        state.serialize_field("replay_kind", replay_kind_name(&self.record))?;
        state.serialize_field("raw_path", &raw_path)?;
        state.serialize_field("content_hash", &self.record.content_hash)?;
        state.end()
    }
}

#[derive(Debug, Serialize)]
pub(crate) struct RefreshedSource {
    pub(crate) old_id: String,
    pub(crate) new_id: String,
    pub(crate) location: String,
    pub(crate) source_kind: SourceKind,
    pub(crate) replay_kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) final_url: Option<String>,
    pub(crate) raw_path: PathBuf,
    pub(crate) previous_raw_path: PathBuf,
    pub(crate) removed_paths: Vec<PathBuf>,
    pub(crate) changed: bool,
    pub(crate) source: SourceRecord,
}

#[derive(Debug, Serialize)]
pub(crate) struct RefreshResult {
    pub(crate) id: String,
    pub(crate) location: String,
    pub(crate) source_kind: SourceKind,
    pub(crate) replay_kind: &'static str,
    pub(crate) raw_path: PathBuf,
    pub(crate) content_hash: String,
    /// Serialized as `false` so unchanged entries keep the same output shape as
    /// changed refresh entries.
    pub(crate) changed: bool,
}

#[derive(Debug, Serialize)]
pub(crate) struct RefreshFailure {
    pub(crate) id: String,
    pub(crate) location: Option<String>,
    pub(crate) source_kind: Option<SourceKind>,
    pub(crate) code: String,
    pub(crate) message: String,
}

#[derive(Debug, Serialize)]
pub(crate) struct SkippedRefresh {
    pub(crate) id: String,
    pub(crate) location: String,
    pub(crate) source_kind: SourceKind,
    pub(crate) code: String,
    pub(crate) message: String,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct IndexedCounts {
    pub(crate) documents: usize,
    pub(crate) chunks: usize,
    pub(crate) links: usize,
    pub(crate) sources: usize,
    pub(crate) ingestions: usize,
}

impl From<IndexCounts> for IndexedCounts {
    fn from(counts: IndexCounts) -> Self {
        Self {
            documents: counts.documents,
            chunks: counts.chunks,
            links: counts.links,
            sources: counts.sources,
            ingestions: counts.ingestions,
        }
    }
}

#[derive(Debug, Serialize)]
pub(crate) struct IndexStatus {
    pub(crate) status: &'static str,
    pub(crate) index_required: bool,
    pub(crate) indexed: Option<IndexedCounts>,
}

impl IndexStatus {
    pub(crate) fn not_run() -> Self {
        Self {
            status: "not_run",
            index_required: false,
            indexed: None,
        }
    }

    pub(crate) fn indexed(indexed: IndexedCounts) -> Self {
        Self {
            status: "indexed",
            index_required: false,
            indexed: Some(indexed),
        }
    }

    pub(crate) fn degraded() -> Self {
        Self {
            status: "degraded",
            index_required: false,
            indexed: None,
        }
    }
}
