use scraper::{ElementRef, Html, Node, Selector};

use crate::document::{DocumentFailureMode, DocumentUnitCount};
use crate::ingest::{single_line, text_from_utf8_lossy};

use super::*;

pub(crate) fn extract_html_document(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
    let html = text_from_utf8_lossy(bytes);
    let document = Html::parse_document(&html);
    let title = extract_html_title(&document);
    let root = Selector::parse("body")
        .ok()
        .and_then(|selector| document.select(&selector).next())
        .unwrap_or_else(|| document.root_element());
    let mut parts = Vec::new();
    collect_visible_text(root, &mut parts);
    let markdown = normalize_markdown_text(&parts.join("\n"));
    if markdown.is_empty() {
        return Ok(DocumentExtraction {
            title,
            markdown,
            units_label: "section_count",
            units_count: 0,
            degradation: Some(DocumentDegradation::new(
                DocumentFailureMode::HtmlNoContent,
                DocumentUnitCount::pages(1),
                "HTML contained no readable text; original asset is preserved.",
            )),
        });
    }
    Ok(DocumentExtraction {
        title,
        markdown,
        units_label: "section_count",
        units_count: 1,
        degradation: None,
    })
}

fn extract_html_title(document: &Html) -> Option<String> {
    let selector = Selector::parse("title").ok()?;
    let title = document
        .select(&selector)
        .next()?
        .text()
        .collect::<Vec<_>>()
        .join(" ");
    let title = markdown_title(&decode_xml_entities(&title));
    (!title.is_empty()).then_some(title)
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
                    if is_block_element(child_element.value().name()) {
                        push_visible_part(parts, &mut inline);
                        collect_visible_text(child_element, parts);
                    } else {
                        let child_text = collect_inline_text(child_element);
                        append_inline_text(&mut inline, &child_text);
                    }
                }
            }
            _ => {}
        }
    }
    push_visible_part(parts, &mut inline);
}

fn collect_inline_text(element: ElementRef<'_>) -> String {
    if matches!(element.value().name(), "head" | "script" | "style") {
        return String::new();
    }
    let mut text = String::new();
    for child in element.children() {
        match child.value() {
            Node::Text(node_text) => append_inline_text(&mut text, &node_text.text),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    let child_text = collect_inline_text(child_element);
                    append_inline_text(&mut text, &child_text);
                }
            }
            _ => {}
        }
    }
    text
}

fn append_inline_text(output: &mut String, text: &str) {
    let text = text.trim();
    if text.is_empty() {
        return;
    }
    if !output.is_empty()
        && output.chars().last().is_some_and(|ch| !ch.is_whitespace())
        && !starts_with_closing_punctuation(text)
    {
        output.push(' ');
    }
    output.push_str(text);
}

fn starts_with_closing_punctuation(text: &str) -> bool {
    text.chars().next().is_some_and(|ch| {
        matches!(
            ch,
            '.' | ','
                | ';'
                | ':'
                | '!'
                | '?'
                | ')'
                | ']'
                | '}'
                | '"'
                | '\''
                | '\u{201d}'
                | '\u{2019}'
                | '\u{203a}'
                | '\u{00bb}'
                | '\u{3002}'
                | '\u{ff0c}'
                | '\u{3001}'
                | '\u{ff09}'
                | '\u{3011}'
                | '\u{3015}'
                | '\u{3017}'
                | '\u{300b}'
        )
    })
}

fn push_visible_part(parts: &mut Vec<String>, inline: &mut String) {
    let part = single_line(inline);
    if !part.is_empty() {
        parts.push(part);
    }
    inline.clear();
}

fn is_block_element(name: &str) -> bool {
    matches!(
        name,
        "address"
            | "article"
            | "aside"
            | "blockquote"
            | "br"
            | "dd"
            | "div"
            | "dl"
            | "dt"
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
            | "hr"
            | "li"
            | "main"
            | "nav"
            | "ol"
            | "p"
            | "pre"
            | "section"
            | "table"
            | "td"
            | "th"
            | "tr"
            | "ul"
    )
}

fn normalize_markdown_text(text: &str) -> String {
    decode_xml_entities(text)
        .lines()
        .map(single_line)
        .filter(|line| !line.is_empty())
        .fold(Vec::<String>::new(), |mut lines, line| {
            if lines.last() != Some(&line) {
                lines.push(line);
            }
            lines
        })
        .join("\n\n")
}
