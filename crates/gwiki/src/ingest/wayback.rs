use std::path::Path;

use crate::WikiError;
use crate::ingest::{
    IngestResult, markdown_metadata, markdown_title, single_line, text_from_utf8_lossy,
    write_raw_then_index,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WaybackCaptureSnapshot {
    pub original_url: String,
    pub capture_url: String,
    pub capture_timestamp: String,
    pub fetched_at: String,
    pub body: Vec<u8>,
    pub content_type: Option<String>,
}

pub fn ingest_capture(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: WaybackCaptureSnapshot,
) -> Result<IngestResult, WikiError> {
    let title = markdown_title(&snapshot.original_url);
    let draft = SourceDraft {
        location: snapshot.capture_url.clone(),
        kind: SourceKind::Wayback,
        fetched_at: snapshot.fetched_at.clone(),
        content: snapshot.body.clone(),
        title: Some(title.clone()),
        citation: Some(snapshot.capture_url.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render_wayback_markdown(&snapshot, &title, &record.content_hash);
    write_raw_then_index(vault_root, store, record, &markdown, None)
}

fn render_wayback_markdown(
    snapshot: &WaybackCaptureSnapshot,
    title: &str,
    source_hash: &str,
) -> String {
    let mut fields = vec![
        ("source_kind", "wayback".to_string()),
        ("original_url", snapshot.original_url.clone()),
        ("capture_url", snapshot.capture_url.clone()),
        ("capture_timestamp", snapshot.capture_timestamp.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
    ];
    if let Some(content_type) = &snapshot.content_type {
        fields.push(("content_type", content_type.clone()));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    markdown.push_str(&html_to_text(&snapshot.body));
    if !markdown.ends_with('\n') {
        markdown.push('\n');
    }
    markdown
}

fn html_to_text(bytes: &[u8]) -> String {
    let html = text_from_utf8_lossy(bytes);
    let html = strip_script_style(&html);
    let mut output = String::new();
    let mut in_tag = false;
    for ch in html.chars() {
        match ch {
            '<' => in_tag = true,
            '>' => {
                in_tag = false;
                output.push(' ');
            }
            _ if !in_tag => output.push(ch),
            _ => {}
        }
    }
    let decoded = html_escape::decode_html_entities(&output);
    decoded
        .lines()
        .map(single_line)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn strip_script_style(html: &str) -> String {
    let lower = html.to_ascii_lowercase();
    let mut output = String::new();
    let mut cursor = 0usize;

    while cursor < html.len() {
        let rest = &lower[cursor..];
        let script = rest.find("<script").map(|offset| (offset, "script"));
        let style = rest.find("<style").map(|offset| (offset, "style"));
        let Some((offset, tag)) = [script, style]
            .into_iter()
            .flatten()
            .min_by_key(|(offset, _)| *offset)
        else {
            output.push_str(&html[cursor..]);
            break;
        };

        let start = cursor + offset;
        output.push_str(&html[cursor..start]);
        let open_end = lower[start..]
            .find('>')
            .map(|offset| start + offset + 1)
            .unwrap_or(html.len());
        let close = format!("</{tag}");
        let Some(close_start) = lower[open_end..]
            .find(&close)
            .map(|offset| open_end + offset)
        else {
            break;
        };
        cursor = lower[close_start..]
            .find('>')
            .map(|offset| close_start + offset + 1)
            .unwrap_or(html.len());
    }

    output
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;

    #[test]
    fn wayback_records_capture_metadata() {
        let temp = tempfile::tempdir().expect("tempdir");
        let body = b"<html><head><style>.hidden{display:none}</style><script>ignore()</script></head><body><main>Archived &amp; decoded page body.</main></body></html>".to_vec();
        let snapshot = WaybackCaptureSnapshot {
            original_url: "https://example.com/research".to_string(),
            capture_url: "https://web.archive.org/web/20260529123456/https://example.com/research"
                .to_string(),
            capture_timestamp: "20260529123456".to_string(),
            fetched_at: "2026-05-29T18:10:00Z".to_string(),
            body,
            content_type: Some("text/html".to_string()),
        };
        let mut store = MemoryWikiStore::default();

        let result = ingest_capture(temp.path(), &mut store, snapshot).expect("ingest wayback");

        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("# https://example.com/research"));
        assert!(raw.contains("source_kind: wayback"));
        assert!(raw.contains("original_url: https://example.com/research"));
        assert!(raw.contains(
            "capture_url: https://web.archive.org/web/20260529123456/https://example.com/research"
        ));
        assert!(raw.contains("capture_timestamp: 20260529123456"));
        assert!(raw.contains("fetched_at: 2026-05-29T18:10:00Z"));
        assert!(raw.contains("Archived & decoded page body."));
        assert!(!raw.contains("ignore()"));
        assert!(!raw.contains("display:none"));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        let entry = &manifest.entries[0];
        assert_eq!(entry.kind, SourceKind::Wayback);
        assert_eq!(
            entry.citation.as_deref(),
            Some("https://web.archive.org/web/20260529123456/https://example.com/research")
        );
        assert_eq!(entry.fetched_at, "2026-05-29T18:10:00Z");
    }
}
