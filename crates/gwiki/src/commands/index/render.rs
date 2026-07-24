use std::path::Path;

use serde_json::json;

use crate::ingest::{self, IngestResult};
use crate::support::counts::IndexCounts;
use crate::{CommandOutcome, ScopeIdentity};

pub(super) fn render_ingest_file(
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
        "Ingested file
Scope: {scope}
Raw: {}
Asset: {}
Source: {} ({})
Content hash: {}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        ingest::path_to_string(&result.raw_path),
        result
            .asset_path
            .as_ref()
            .map(|path| ingest::path_to_string(path))
            .unwrap_or_else(|| "<none>".to_string()),
        result.record.location,
        result.record.kind,
        result.record.content_hash,
        counts.documents,
        counts.chunks,
        counts.links,
        counts.sources,
        counts.ingestions
    );
    super::super::scoped_outcome("ingest-file", &scope, payload, text)
}

pub(super) fn render_ingest_url(
    scope: ScopeIdentity,
    result: &ingest::url::UrlBatchIngest,
    counts: IndexCounts,
) -> CommandOutcome {
    let accepted = result
        .accepted
        .iter()
        .map(|accepted| {
            json!({
                "requested_url": &accepted.requested_url,
                "final_url": &accepted.final_url,
                "raw_path": &accepted.result.raw_path,
                "source": {
                    "id": &accepted.result.record.id,
                    "kind": &accepted.result.record.kind,
                    "content_hash": &accepted.result.record.content_hash,
                    "location": &accepted.result.record.location,
                },
            })
        })
        .collect::<Vec<_>>();
    let cached = result
        .cached
        .iter()
        .map(|cached| {
            json!({
                "requested_url": &cached.requested_url,
                "source_id": &cached.source_id,
                "fetched_at": &cached.fetched_at,
                "age_hours": cached.age_hours,
            })
        })
        .collect::<Vec<_>>();
    let failed = result
        .failed
        .iter()
        .map(|failure| {
            json!({
                "url": &failure.url,
                "code": &failure.code,
                "message": &failure.message,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "ingest-url",
        "scope": scope,
        "status": result.status(),
        "accepted": accepted,
        "cached": cached,
        "failed": failed,
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Ingested URLs
Scope: {scope}
Status: {}
Accepted: {}
Cached: {}
Failed: {}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        result.status(),
        result.accepted.len(),
        result.cached.len(),
        result.failed.len(),
        counts.documents,
        counts.chunks,
        counts.links,
        counts.sources,
        counts.ingestions
    );
    let mut outcome = super::super::scoped_outcome("ingest-url", &scope, payload, text);
    outcome.exit_code = result.exit_code();
    outcome
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::render_ingest_url;
    use crate::ScopeIdentity;
    use crate::ingest::url::{CachedUrlIngest, UrlBatchIngest};
    use crate::support::counts::IndexCounts;

    #[test]
    fn ingest_url_render_includes_cached_entries_and_count() {
        let result = UrlBatchIngest {
            accepted: Vec::new(),
            cached: vec![CachedUrlIngest {
                requested_url: "https://example.test/source".to_string(),
                source_id: "url-source".to_string(),
                fetched_at: "2026-07-24T00:00:00Z".to_string(),
                age_hours: 3,
            }],
            failed: Vec::new(),
        };

        let outcome = render_ingest_url(
            ScopeIdentity::global(),
            &result,
            IndexCounts {
                documents: 1,
                chunks: 2,
                links: 3,
                sources: 4,
                ingestions: 5,
            },
        );

        assert_eq!(outcome.result.payload["status"], "ingested");
        assert_eq!(
            outcome.result.payload["cached"],
            json!([{
                "requested_url": "https://example.test/source",
                "source_id": "url-source",
                "fetched_at": "2026-07-24T00:00:00Z",
                "age_hours": 3,
            }])
        );
        assert!(outcome.result.text.lines().any(|line| line == "Cached: 1"));
        assert_eq!(outcome.exit_code, 0);
    }
}
