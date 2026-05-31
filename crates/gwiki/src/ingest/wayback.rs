use std::path::Path;
use std::sync::OnceLock;

use regex::Regex;
use scraper::{Html, Selector};

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
    let output = extract_html_text(&strip_script_style(&html));
    output
        .lines()
        .map(single_line)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn extract_html_text(html: &str) -> String {
    let document = Html::parse_document(html);
    let body_selector = Selector::parse("body").expect("body selector parses");
    document
        .select(&body_selector)
        .next()
        .map(|body| body.text().collect::<Vec<_>>().join("\n"))
        .unwrap_or_else(|| {
            document
                .root_element()
                .text()
                .collect::<Vec<_>>()
                .join("\n")
        })
}

fn strip_script_style(html: &str) -> String {
    static SCRIPT_RE: OnceLock<Regex> = OnceLock::new();
    static STYLE_RE: OnceLock<Regex> = OnceLock::new();

    let script_re = SCRIPT_RE.get_or_init(|| {
        Regex::new(r"(?is)<script\b[^>]*>.*?</script\s*>")
            .expect("script-stripping regex should compile")
    });
    let style_re = STYLE_RE.get_or_init(|| {
        Regex::new(r"(?is)<style\b[^>]*>.*?</style\s*>")
            .expect("style-stripping regex should compile")
    });

    let without_scripts = script_re.replace_all(html, " ");
    style_re.replace_all(&without_scripts, " ").into_owned()
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

    #[test]
    fn wayback_extracts_body_text_without_head_metadata() {
        let body = b"<html><head><title>Archive Shell</title></head><body><script>ignore()</script><p>Visible &amp; decoded.</p></body></html>";

        let text = html_to_text(body);

        assert_eq!(text, "Visible & decoded.");
        assert!(!text.contains("Archive Shell"));
        assert!(!text.contains("ignore()"));
    }

    #[test]
    fn wayback_does_not_decode_entities_twice() {
        let body = b"<html><body><p>Use &amp;amp; literally.</p></body></html>";

        let text = html_to_text(body);

        assert_eq!(text, "Use &amp; literally.");
    }
}
