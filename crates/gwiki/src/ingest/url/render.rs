use std::path::Path;

use scraper::{ElementRef, Html, Node, Selector};

use crate::ingest::{
    markdown_metadata, markdown_title, path_to_string, single_line, text_from_utf8_lossy,
};
use crate::sources::SourceKind;

use super::UrlSnapshot;

pub(super) fn render_url_markdown(
    snapshot: &UrlSnapshot,
    canonical_url: &str,
    title: &str,
    document: &Html,
    source_hash: &str,
) -> String {
    let mut fields = vec![
        ("source_kind", "url".to_string()),
        ("source_url", snapshot.final_url.clone()),
        ("requested_url", snapshot.requested_url.clone()),
        ("canonical_url", canonical_url.to_string()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
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

pub(super) fn render_non_html_url_markdown(
    snapshot: &UrlSnapshot,
    canonical_url: &str,
    title: &str,
    kind: &SourceKind,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let mut fields = vec![
        ("source_kind", kind.to_string()),
        ("source_url", snapshot.final_url.clone()),
        ("requested_url", snapshot.requested_url.clone()),
        ("canonical_url", canonical_url.to_string()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", path_to_string(asset_path)),
        ("media_degradation", "url_non_html_asset".to_string()),
    ];
    if let Some(content_type) = &snapshot.content_type {
        fields.push(("content_type", content_type.clone()));
    }
    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(title));
    markdown.push_str("\n\n");
    markdown.push_str("Non-HTML URL response preserved as a source asset.\n");
    markdown
}

pub(super) fn snapshot_is_html(snapshot: &UrlSnapshot) -> bool {
    match content_type_media_type(snapshot.content_type.as_deref()).as_deref() {
        Some("text/html" | "application/xhtml+xml") => true,
        Some(_) => false,
        None => body_looks_like_html(&snapshot.body),
    }
}

pub(super) fn source_kind_for_url_response(content_type: Option<&str>) -> SourceKind {
    match content_type_media_type(content_type).as_deref() {
        Some("application/pdf") => SourceKind::Pdf,
        Some(media_type) if media_type.starts_with("image/") => SourceKind::Image,
        Some(media_type) if media_type.starts_with("audio/") => SourceKind::Audio,
        Some(media_type) if media_type.starts_with("video/") => SourceKind::Video,
        Some("application/json" | "application/xml" | "text/plain" | "text/csv" | "text/xml") => {
            SourceKind::Text
        }
        Some(media_type) if media_type.starts_with("text/") => SourceKind::Text,
        _ => SourceKind::File,
    }
}

fn content_type_media_type(content_type: Option<&str>) -> Option<String> {
    content_type?
        .split(';')
        .next()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_lowercase)
}

fn body_looks_like_html(body: &[u8]) -> bool {
    let text = text_from_utf8_lossy(&body[..body.len().min(512)]).to_ascii_lowercase();
    let trimmed = text.trim_start();
    trimmed.starts_with("<!doctype html")
        || trimmed.starts_with("<html")
        || trimmed.contains("<body")
}

pub(super) fn file_name_for_url_response(snapshot: &UrlSnapshot, kind: &SourceKind) -> String {
    let from_url = url::Url::parse(&snapshot.final_url)
        .ok()
        .and_then(|url| {
            url.path_segments()
                .and_then(|mut segments| segments.next_back().map(str::to_string))
        })
        .filter(|value| !value.trim().is_empty());
    from_url.unwrap_or_else(|| match kind {
        SourceKind::Pdf => "download.pdf".to_string(),
        SourceKind::Image => "image".to_string(),
        SourceKind::Audio => "audio".to_string(),
        SourceKind::Video => "video".to_string(),
        SourceKind::Text => "download.txt".to_string(),
        _ => "download".to_string(),
    })
}

pub(super) fn extract_title(document: &Html) -> Option<String> {
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

pub(super) fn html_to_markdownish_text(document: &Html) -> String {
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
    if is_hidden_element(element.value().name()) {
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
