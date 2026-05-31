use std::path::Path;
use std::sync::OnceLock;

use percent_encoding::percent_decode_str;
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
    let title = wayback_title(&snapshot);
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

fn wayback_title(snapshot: &WaybackCaptureSnapshot) -> String {
    html_title(&snapshot.body)
        .or_else(|| title_from_url_path(&snapshot.original_url))
        .or_else(|| url_host(&snapshot.original_url).map(|host| markdown_title(&host)))
        .unwrap_or_else(|| markdown_title(&snapshot.original_url))
}

fn html_title(bytes: &[u8]) -> Option<String> {
    let html = text_from_utf8_lossy(bytes);
    let document = Html::parse_document(&html);
    let title_selector = Selector::parse("title").expect("title selector parses");
    document
        .select(&title_selector)
        .next()
        .map(|title| markdown_title(&single_line(&title.text().collect::<Vec<_>>().join(" "))))
        .filter(|title| !title.is_empty())
}

fn title_from_url_path(url: &str) -> Option<String> {
    let without_fragment = url.split('#').next().unwrap_or(url);
    let without_query = without_fragment
        .split('?')
        .next()
        .unwrap_or(without_fragment);
    let path = without_query
        .split_once("://")
        .and_then(|(_, rest)| rest.split_once('/').map(|(_, path)| path))
        .unwrap_or(without_query);
    let segment = path.trim_end_matches('/').rsplit('/').next()?.trim();
    if segment.is_empty() {
        return None;
    }
    let title = markdown_title(&percent_decode_lossy(segment));
    (!title.is_empty()).then_some(title)
}

fn url_host(url: &str) -> Option<String> {
    let host = url
        .split_once("://")?
        .1
        .split(['/', '?', '#'])
        .next()?
        .trim();
    (!host.is_empty()).then(|| host.to_string())
}

fn percent_decode_lossy(value: &str) -> String {
    percent_decode_str(value).decode_utf8_lossy().into_owned()
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
        assert!(raw.contains("# research"));
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
    fn wayback_prefers_html_title_then_decoded_url_path_then_host() {
        let with_title = WaybackCaptureSnapshot {
            original_url: "https://example.com/fallback".to_string(),
            capture_url: "https://web.archive.org/web/1/https://example.com/fallback".to_string(),
            capture_timestamp: "1".to_string(),
            fetched_at: "2026-05-29T18:10:00Z".to_string(),
            body: b"<html><head><title>Readable Title</title></head><body></body></html>".to_vec(),
            content_type: Some("text/html".to_string()),
        };
        let without_title = WaybackCaptureSnapshot {
            original_url: "https://example.com/research/hello%20world?utm=1#frag".to_string(),
            body: b"<html><body>No title</body></html>".to_vec(),
            ..with_title.clone()
        };
        let host_only = WaybackCaptureSnapshot {
            original_url: "https://example.com/?utm=1#frag".to_string(),
            body: b"<html><body>No title</body></html>".to_vec(),
            ..with_title.clone()
        };

        assert_eq!(wayback_title(&with_title), "Readable Title");
        assert_eq!(wayback_title(&without_title), "hello world");
        assert_eq!(wayback_title(&host_only), "example.com");
    }

    #[test]
    fn wayback_does_not_decode_entities_twice() {
        let body = b"<html><body><p>Use &amp;amp; literally.</p></body></html>";

        let text = html_to_text(body);

        assert_eq!(text, "Use &amp; literally.");
    }
}
