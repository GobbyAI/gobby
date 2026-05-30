use std::path::Path;

use serde_json::json;

use crate::CommandOutcome;
use crate::IndexCounts;
use crate::ScopeIdentity;
use crate::ingest::IngestResult;

pub(crate) fn run(scope: ScopeIdentity, root: &Path, counts: IndexCounts) -> CommandOutcome {
    let payload = json!({
        "command": "index",
        "scope": scope,
        "status": "indexed",
        "root": root,
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Index complete\nScope: {scope}\nDocuments: {}\nChunks: {}\nLinks: {}",
        counts.documents, counts.chunks, counts.links
    );
    super::scoped_outcome("index", &scope, payload, text)
}

pub(crate) fn ingest_file(
    path: &Path,
    scope: ScopeIdentity,
    result: &IngestResult,
    counts: IndexCounts,
) -> CommandOutcome {
    let payload = json!({
        "command": "ingest-file",
        "scope": scope,
        "status": "ingested",
        "path": path,
        "raw_path": &result.raw_path,
        "asset_path": &result.asset_path,
        "source": {
            "id": &result.record.id,
            "kind": &result.record.kind,
            "content_hash": &result.record.content_hash,
            "location": &result.record.location,
        },
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Ingested file\nScope: {scope}\nRaw: {}",
        result.raw_path.display()
    );
    super::scoped_outcome("ingest-file", &scope, payload, text)
}
