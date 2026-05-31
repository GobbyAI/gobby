use std::path::Path;

use crate::WikiError;
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, single_line,
    text_from_utf8_lossy, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UrlSnapshot {
    pub requested_url: String,
    pub final_url: String,
    pub fetched_at: String,
    pub body: Vec<u8>,
    pub content_type: Option<String>,
}

pub fn ingest_snapshot(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: UrlSnapshot,
) -> Result<IngestResult, WikiError> {
    let title = extract_title(&snapshot.body).unwrap_or_else(|| snapshot.final_url.clone());
    let draft = SourceDraft {
        location: snapshot.final_url.clone(),
        kind: SourceKind::Url,
        fetched_at: snapshot.fetched_at.clone(),
        content: snapshot.body.clone(),
        title: Some(markdown_title(&title)),
        citation: Some(snapshot.final_url.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render_url_markdown(&snapshot, &record.canonical_location, &title);
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}

fn render_url_markdown(snapshot: &UrlSnapshot, canonical_url: &str, title: &str) -> String {
    let mut fields = vec![
        ("source_kind", "url".to_string()),
        ("source_url", snapshot.final_url.clone()),
        ("requested_url", snapshot.requested_url.clone()),
        ("canonical_url", canonical_url.to_string()),
        ("fetched_at", snapshot.fetched_at.clone()),
        (
            "source_hash",
            gobby_core::indexing::content_hash(&snapshot.body),
        ),
    ];
    if let Some(content_type) = &snapshot.content_type {
        fields.push(("content_type", content_type.clone()));
    }
    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(title));
    markdown.push_str("\n\n");
    markdown.push_str(&html_to_markdownish_text(&snapshot.body));
    markdown.push('\n');
    markdown
}

fn extract_title(bytes: &[u8]) -> Option<String> {
    let html = text_from_utf8_lossy(bytes);
    let lower = html.to_ascii_lowercase();
    let start = lower.find("<title")?;
    let content_start = lower[start..].find('>')? + start + 1;
    let content_end = lower[content_start..].find("</title>")? + content_start;
    let title = decode_html_entities(&html[content_start..content_end]);
    let title = single_line(&title);
    (!title.is_empty()).then_some(title)
}

fn html_to_markdownish_text(bytes: &[u8]) -> String {
    let html = text_from_utf8_lossy(bytes);
    let without_blocks = remove_tag_block(
        &remove_tag_block(&remove_tag_block(&html, "script"), "style"),
        "head",
    );
    let text = strip_tags(&without_blocks);
    normalize_markdown_text(&decode_html_entities(&text))
}

fn remove_tag_block(html: &str, tag: &str) -> String {
    let mut output = String::new();
    let mut cursor = 0;
    let lower = html.to_ascii_lowercase();
    let open = format!("<{tag}");
    let close = format!("</{tag}>");
    let mut search_cursor = cursor;

    while let Some(start_offset) = lower[search_cursor..].find(&open) {
        let start = search_cursor + start_offset;
        let name_end = start + open.len();
        if !is_tag_name_boundary(lower[name_end..].chars().next()) {
            search_cursor = name_end;
            continue;
        }
        output.push_str(&html[cursor..start]);
        let Some(end_offset) = lower[start..].find(&close) else {
            output.push_str(&html[start..]);
            return output;
        };
        cursor = start + end_offset + close.len();
        search_cursor = cursor;
    }
    output.push_str(&html[cursor..]);
    output
}

fn is_tag_name_boundary(next: Option<char>) -> bool {
    next.is_none_or(|ch| ch == '>' || ch == '/' || ch.is_ascii_whitespace())
}

fn strip_tags(html: &str) -> String {
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
    output
}

fn normalize_markdown_text(text: &str) -> String {
    let mut lines = Vec::new();
    for line in text.lines() {
        let line = single_line(line);
        if !line.is_empty() && lines.last().is_none_or(|last: &String| last != &line) {
            lines.push(line);
        }
    }
    lines.join("\n\n")
}

fn decode_html_entities(text: &str) -> String {
    html_escape::decode_html_entities(text).into_owned()
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;

    #[test]
    fn url_ingest_writes_raw_and_manifest() {
        let temp = tempfile::tempdir().expect("tempdir");
        let body = br#"<!doctype html>
<html>
<head><title>Durable Wikis</title></head>
<body><main><h1>Durable Wikis</h1><p>Capture source material.</p></main></body>
</html>"#
            .to_vec();
        let expected_hash = content_hash(&body);
        let snapshot = UrlSnapshot {
            requested_url: "https://Example.com/docs/wiki#overview".to_string(),
            final_url: "https://example.com/docs/wiki/".to_string(),
            fetched_at: "2026-05-29T16:00:00Z".to_string(),
            body,
            content_type: Some("text/html".to_string()),
        };
        let mut store = MemoryWikiStore::default();

        let result =
            ingest_snapshot(temp.path(), &mut store, snapshot).expect("ingest url snapshot");

        assert_eq!(result.asset_path, None);
        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("# Durable Wikis"));
        assert!(raw.contains("canonical_url: https://example.com/docs/wiki"));
        assert!(raw.contains("fetched_at: 2026-05-29T16:00:00Z"));
        assert!(raw.contains("content_type: text/html"));
        assert!(raw.contains(&format!("source_hash: {expected_hash}")));
        assert!(raw.contains("Capture source material."));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        let entry = &manifest.entries[0];
        assert_eq!(entry.kind, SourceKind::Url);
        assert_eq!(entry.title.as_deref(), Some("Durable Wikis"));
        assert_eq!(entry.canonical_location, "https://example.com/docs/wiki");
        assert_eq!(entry.content_hash, expected_hash);
        assert_eq!(entry.fetched_at, "2026-05-29T16:00:00Z");
        assert!(store.documents.contains_key(&PathBuf::from("raw/INDEX.md")));
    }

    #[test]
    fn remove_tag_block_respects_tag_name_boundaries() {
        let html = "<scripture>keep</scripture><script>drop()</script><script type=\"x\">also_drop()</script>";

        let stripped = remove_tag_block(html, "script");

        assert!(stripped.contains("<scripture>keep</scripture>"));
        assert!(!stripped.contains("drop()"));
        assert!(!stripped.contains("also_drop()"));
    }

    #[test]
    fn remove_tag_block_preserves_tail_when_close_tag_is_missing() {
        let html = "<main>keep</main><script>unterminated<div>tail</div>";

        let stripped = remove_tag_block(html, "script");

        assert!(stripped.contains("<main>keep</main>"));
        assert!(stripped.contains("<script>unterminated<div>tail</div>"));
    }
}
