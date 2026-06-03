use std::path::Path;

use percent_encoding::percent_decode_str;
use scraper::{ElementRef, Html, Node, Selector};
use url::Url;

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
    mut snapshot: WaybackCaptureSnapshot,
) -> Result<IngestResult, WikiError> {
    let html = text_from_utf8_lossy(&snapshot.body);
    let document = Html::parse_document(&html);
    let title = wayback_title(&snapshot, &document);
    let draft = SourceDraft {
        location: snapshot.capture_url.clone(),
        kind: SourceKind::Wayback,
        fetched_at: snapshot.fetched_at.clone(),
        content: std::mem::take(&mut snapshot.body),
        title: Some(title.clone()),
        citation: Some(snapshot.capture_url.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render_wayback_markdown(&snapshot, &document, &title, &record.content_hash);
    write_raw_then_index(vault_root, store, record, &markdown, None)
}

fn wayback_title(snapshot: &WaybackCaptureSnapshot, document: &Html) -> String {
    html_title(document)
        .or_else(|| title_from_url_path(&snapshot.original_url))
        .or_else(|| url_host(&snapshot.original_url).map(|host| markdown_title(&host)))
        .unwrap_or_else(|| markdown_title(&snapshot.original_url))
}

fn html_title(document: &Html) -> Option<String> {
    let title_selector = Selector::parse("title").expect("title selector parses");
    document
        .select(&title_selector)
        .next()
        .map(|title| markdown_title(&single_line(&title.text().collect::<Vec<_>>().join(" "))))
        .filter(|title| !title.is_empty())
}

fn title_from_url_path(url: &str) -> Option<String> {
    let url = Url::parse(url).ok()?;
    let segment = url.path_segments()?.rfind(|segment| !segment.is_empty())?;
    let title = markdown_title(&percent_decode_lossy(segment));
    (!title.is_empty()).then_some(title)
}

fn url_host(url: &str) -> Option<String> {
    Url::parse(url)
        .ok()?
        .host_str()
        .filter(|host| !host.is_empty())
        .map(str::to_string)
}

fn percent_decode_lossy(value: &str) -> String {
    percent_decode_str(value).decode_utf8_lossy().into_owned()
}

fn render_wayback_markdown(
    snapshot: &WaybackCaptureSnapshot,
    document: &Html,
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
    markdown.push_str(&html_to_text(document));
    if !markdown.ends_with('\n') {
        markdown.push('\n');
    }
    markdown
}

fn html_to_text(document: &Html) -> String {
    let output = extract_html_text(document);
    output
        .lines()
        .map(single_line)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn extract_html_text(document: &Html) -> String {
    let body_selector = Selector::parse("body").expect("body selector parses");
    let mut parts = Vec::new();
    if let Some(body) = document.select(&body_selector).next() {
        collect_visible_text(body, &mut parts);
    } else {
        collect_visible_text(document.root_element(), &mut parts);
    }
    parts.join("\n")
}

fn collect_visible_text(element: ElementRef<'_>, parts: &mut Vec<String>) {
    if matches!(element.value().name(), "head" | "script" | "style") {
        return;
    }
    let mut inline = String::new();
    for child in element.children() {
        match child.value() {
            Node::Text(text) => append_inline_text(&mut inline, &text.text),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    let name = child_element.value().name();
                    if name == "br" {
                        push_text_part(parts, &mut inline);
                    } else if is_block_boundary(name) {
                        push_text_part(parts, &mut inline);
                        collect_visible_text(child_element, parts);
                    } else {
                        collect_inline_text(child_element, parts, &mut inline);
                    }
                }
            }
            _ => {}
        }
    }
    push_text_part(parts, &mut inline);
}

fn collect_inline_text(element: ElementRef<'_>, parts: &mut Vec<String>, inline: &mut String) {
    if matches!(element.value().name(), "head" | "script" | "style") {
        return;
    }
    for child in element.children() {
        match child.value() {
            Node::Text(text) => append_inline_text(inline, &text.text),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    let name = child_element.value().name();
                    if name == "br" {
                        push_text_part(parts, inline);
                    } else if is_block_boundary(name) {
                        push_text_part(parts, inline);
                        collect_visible_text(child_element, parts);
                    } else {
                        collect_inline_text(child_element, parts, inline);
                    }
                }
            }
            _ => {}
        }
    }
}

fn append_inline_text(inline: &mut String, text: &str) {
    let text = single_line(text);
    if text.is_empty() {
        return;
    }
    if !inline.is_empty() {
        inline.push(' ');
    }
    inline.push_str(&text);
}

fn push_text_part(parts: &mut Vec<String>, inline: &mut String) {
    let text = single_line(inline);
    inline.clear();
    if !text.is_empty() {
        parts.push(text);
    }
}

fn is_block_boundary(name: &str) -> bool {
    matches!(
        name,
        "address"
            | "article"
            | "aside"
            | "blockquote"
            | "div"
            | "dl"
            | "dt"
            | "dd"
            | "figcaption"
            | "figure"
            | "footer"
            | "h1"
            | "h2"
            | "h3"
            | "h4"
            | "h5"
            | "h6"
            | "header"
            | "li"
            | "main"
            | "nav"
            | "ol"
            | "p"
            | "pre"
            | "section"
            | "table"
            | "tbody"
            | "td"
            | "th"
            | "thead"
            | "tr"
            | "ul"
    )
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
        let html = text_from_utf8_lossy(body);
        let document = Html::parse_document(&html);

        let text = html_to_text(&document);

        assert_eq!(text, "Visible & decoded.");
        assert!(!text.contains("Archive Shell"));
        assert!(!text.contains("ignore()"));
    }

    #[test]
    fn wayback_groups_inline_text_per_block() {
        let body = br#"
<html>
  <body>
    <p><span>Inline</span> <strong>siblings</strong> stay together.</p>
    <p>Second<br>line</p>
  </body>
</html>
"#;
        let document = document_for(body);

        let text = html_to_text(&document);

        assert_eq!(text, "Inline siblings stay together.\n\nSecond\n\nline");
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

        assert_eq!(
            wayback_title(&with_title, &document_for(&with_title.body)),
            "Readable Title"
        );
        assert_eq!(
            wayback_title(&without_title, &document_for(&without_title.body)),
            "hello world"
        );
        assert_eq!(
            wayback_title(&host_only, &document_for(&host_only.body)),
            "example.com"
        );
    }

    #[test]
    fn wayback_does_not_decode_entities_twice() {
        let body = b"<html><body><p>Use &amp;amp; literally.</p></body></html>";
        let document = document_for(body);

        let text = html_to_text(&document);

        assert_eq!(text, "Use &amp; literally.");
    }

    fn document_for(bytes: &[u8]) -> Html {
        let html = text_from_utf8_lossy(bytes);
        Html::parse_document(&html)
    }
}
