use std::path::Path;

use scraper::{ElementRef, Html, Node, Selector};

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
    let html = text_from_utf8_lossy(&snapshot.body);
    let document = Html::parse_document(&html);
    let title = extract_title(&document).unwrap_or_else(|| snapshot.final_url.clone());
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
    let markdown = render_url_markdown(&snapshot, &record.canonical_location, &title, &document);
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}

fn render_url_markdown(
    snapshot: &UrlSnapshot,
    canonical_url: &str,
    title: &str,
    document: &Html,
) -> String {
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
    markdown.push_str(&html_to_markdownish_text(document));
    markdown.push('\n');
    markdown
}

fn extract_title(document: &Html) -> Option<String> {
    let selector = Selector::parse("title").ok()?;
    let title = document
        .select(&selector)
        .next()?
        .text()
        .collect::<Vec<_>>()
        .join(" ");
    let title = single_line(&title);
    (!title.is_empty()).then_some(title)
}

fn html_to_markdownish_text(document: &Html) -> String {
    let mut parts = Vec::new();
    let root = Selector::parse("body")
        .ok()
        .and_then(|selector| document.select(&selector).next())
        .unwrap_or_else(|| document.root_element());
    collect_visible_text(root, &mut parts);
    let text = parts.join("\n");
    normalize_markdown_text(&text)
}

fn collect_visible_text(element: ElementRef<'_>, parts: &mut Vec<String>) {
    if matches!(element.value().name(), "head" | "script" | "style") {
        return;
    }
    if is_text_block(element.value().name()) {
        let mut text = String::new();
        collect_inline_text(element, &mut text);
        if !single_line(&text).is_empty() {
            parts.push(text);
        }
        return;
    }

    let mut inline = String::new();
    for child in element.children() {
        match child.value() {
            Node::Text(text) => inline.push_str(&text.text),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    if is_hidden_element(child_element.value().name()) {
                        continue;
                    }
                    if is_text_block(child_element.value().name()) {
                        push_inline_part(&mut inline, parts);
                        collect_visible_text(child_element, parts);
                    } else {
                        collect_inline_text(child_element, &mut inline);
                    }
                }
            }
            _ => {}
        }
    }
    push_inline_part(&mut inline, parts);
}

fn collect_inline_text(element: ElementRef<'_>, output: &mut String) {
    if is_hidden_element(element.value().name()) {
        return;
    }
    for child in element.children() {
        match child.value() {
            Node::Text(text) => output.push_str(&text.text),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    collect_inline_text(child_element, output);
                }
            }
            _ => {}
        }
    }
}

fn push_inline_part(inline: &mut String, parts: &mut Vec<String>) {
    if !single_line(inline).is_empty() {
        parts.push(std::mem::take(inline));
    } else {
        inline.clear();
    }
}

fn is_hidden_element(name: &str) -> bool {
    matches!(name, "head" | "script" | "style")
}

fn is_text_block(name: &str) -> bool {
    matches!(
        name,
        "address"
            | "blockquote"
            | "dd"
            | "dt"
            | "figcaption"
            | "h1"
            | "h2"
            | "h3"
            | "h4"
            | "h5"
            | "h6"
            | "li"
            | "p"
            | "pre"
            | "td"
            | "th"
    )
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
    fn html_parser_extracts_body_text_and_decodes_entities() {
        let html = br#"<!doctype html>
<html>
<head><title>Hidden &amp; Title</title></head>
<body><main><p>Keep <strong>&amp; decode</strong> together.</p><script>drop()</script></main></body>
</html>"#;

        let html = Html::parse_document(&text_from_utf8_lossy(html));

        assert_eq!(extract_title(&html), Some("Hidden & Title".to_string()));
        assert_eq!(html_to_markdownish_text(&html), "Keep & decode together.");
    }
}
