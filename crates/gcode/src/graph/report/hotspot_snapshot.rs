//! Stored whole-project hotspots for `gcode graph report`.
//!
//! The four hotspot stages (file, symbol, module, incoming calls) each cost more
//! than the 29 s per-query budget on a large overlay, so they are computed on the
//! index path and stored as one snapshot node per overlay scope. A report reads the
//! node when its admitted-version digest and top-n match; otherwise the report
//! computes the same stages live.
//!
//! Writers invalidate every snapshot whose namespaces include the written project,
//! so a stale digest is never served.

use std::collections::HashMap;

use crate::index::indexer::IndexDegradation;
use crate::visibility::graph_versions::AdmittedNamespace;

use super::types::GraphReportHotspots;

/// Bump when the payload encoding or the digest preimage changes.
pub(crate) const SNAPSHOT_FORMAT_VERSION: u32 = 1;

/// One stored snapshot node, read back from the graph.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct StoredSnapshot {
    pub(crate) format: u32,
    pub(crate) top_n: usize,
    pub(crate) digest: String,
    pub(crate) payload: String,
}

/// SHA-256 over the canonical encoding of every admitted namespace: project id,
/// then sorted keys, paths and endpoints, each length-prefixed. The overlay and
/// each source namespace stay separate, so a key that moves between namespaces
/// changes the digest. `pending` is `has_pending_pinned_graph_projection`.
pub(crate) fn admitted_digest(
    namespaces: &[AdmittedNamespace],
    pending: bool,
    format_version: u32,
) -> String {
    let _ = (namespaces, pending, format_version);
    todo!("admitted_digest: canonical per-namespace SHA-256")
}

/// The stored hotspots when the snapshot matches the current digest, format and top-n.
pub(crate) fn stored_hotspots_for(
    stored: Option<&StoredSnapshot>,
    digest: &str,
    top_n: usize,
) -> Option<GraphReportHotspots> {
    let _ = (stored, digest, top_n);
    todo!("stored_hotspots_for: match format, digest and top_n, then decode")
}

/// Encodes merged, top-n-truncated hotspots as the snapshot payload.
pub(crate) fn encode_payload(hotspots: &GraphReportHotspots) -> String {
    let _ = hotspots;
    todo!("encode_payload: serde_json")
}

/// Decodes a payload; `None` when it does not parse.
pub(crate) fn decode_payload(payload: &str) -> Option<GraphReportHotspots> {
    let _ = payload;
    todo!("decode_payload: serde_json")
}

/// Cypher that deletes every snapshot node naming `project_id` in its namespaces.
pub(crate) fn invalidate_snapshots_query(project_id: &str) -> (String, HashMap<String, String>) {
    let _ = project_id;
    todo!("invalidate_snapshots_query: DELETE by namespace membership")
}

/// Runs the invalidation after a writer, whether the writer succeeded or failed:
/// a failed write may have left partial graph facts behind. The writer's error wins;
/// otherwise an invalidation error is returned.
pub(crate) fn invalidate_after_write<T>(
    project_id: &str,
    result: anyhow::Result<T>,
    invalidate: impl FnOnce(&str) -> anyhow::Result<()>,
) -> anyhow::Result<T> {
    let _ = (project_id, result, invalidate);
    todo!("invalidate_after_write: always invalidate, writer error first")
}

/// The visible degradation for a failed snapshot refresh. The index stays successful.
pub(crate) fn refresh_failure_degradation(result: anyhow::Result<()>) -> Option<IndexDegradation> {
    let _ = result;
    todo!("refresh_failure_degradation: Err becomes HotspotSnapshotFailed")
}
