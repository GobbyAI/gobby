use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::ScopeIdentity;
use crate::WikiError;
use crate::document::{DocumentDegradation, DocumentFailureMode, DocumentUnitCount};
use crate::ingest::{markdown_metadata, path_to_string, single_line};
use crate::vision::{VisionEndpoint, VisionExtraction, VisionRequest};

use super::text::normalize_page_text;
use super::types::{PdfRenderedPage, PdfSnapshot};
use super::{PdfMarkdownSummary, PdfPageMarkdown};

pub(crate) fn render_pdf_markdown(
    scope: &ScopeIdentity,
    snapshot: &PdfSnapshot,
    title: &str,
    source_hash: &str,
    asset_path: &Path,
    pages: &[PdfPageMarkdown],
    summary: &PdfMarkdownSummary,
) -> String {
    let mut fields = vec![
        ("source_kind", "pdf".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.to_rfc3339()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", path_to_string(asset_path)),
        ("file_size_bytes", snapshot.bytes.len().to_string()),
        ("page_count", summary.page_count.to_string()),
        ("has_text_layer", summary.has_text_layer.to_string()),
        ("vision_used", summary.vision_used.to_string()),
        (
            "model",
            summary.model.clone().unwrap_or_else(|| "none".to_string()),
        ),
        ("scope_kind", scope.kind.as_str().to_string()),
        ("scope_id", scope.id.clone()),
    ];
    if !summary.degradations.is_empty() {
        fields.push((
            "media_degradation",
            summary
                .degradations
                .iter()
                .map(|degradation| degradation.reason())
                .collect::<Vec<_>>()
                .join("; "),
        ));
    }
    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");

    if !summary.degradations.is_empty() {
        markdown.push_str("## Document Degradation\n\n");
        for degradation in &summary.degradations {
            markdown.push_str("- ");
            markdown.push_str(degradation.reason());
            markdown.push_str(": ");
            markdown.push_str(&single_line(&degradation.fallback));
            markdown.push('\n');
        }
        markdown.push('\n');
    }

    if pages.is_empty() {
        markdown.push_str("No extractable page text.\n");
        return markdown;
    }

    for page in pages {
        markdown.push_str("<!-- gwiki-page: ");
        markdown.push_str(&page.number.to_string());
        markdown.push_str(" -->\n\n");
        markdown.push_str("## Page ");
        markdown.push_str(&page.number.to_string());
        markdown.push_str("\n\n");
        if page.markdown.is_empty() {
            markdown.push_str("No extractable page text.");
        } else {
            markdown.push_str(&sanitize_pdf_page_markdown(&page.markdown));
        }
        markdown.push_str("\n\n");
    }
    markdown
}

pub(crate) fn sanitize_pdf_page_markdown(markdown: &str) -> String {
    markdown
        .lines()
        .map(|line| {
            let trimmed = line.trim();
            let line = if is_markdown_horizontal_rule(trimmed) {
                format!("\\{line}")
            } else {
                line.to_string()
            };
            // User-visible page markdown must not create internal page split markers.
            neutralize_gwiki_page_marker_variants(&line)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn neutralize_gwiki_page_marker_variants(line: &str) -> String {
    let lower = line.to_ascii_lowercase();
    let bytes = lower.as_bytes();
    let mut output = String::new();
    let mut start = 0usize;
    while let Some(offset) = lower[start..].find("<!--") {
        let marker_start = start + offset;
        let mut cursor = marker_start + "<!--".len();
        while bytes
            .get(cursor)
            .is_some_and(|byte| byte.is_ascii_whitespace())
        {
            cursor += 1;
        }
        if lower[cursor..].starts_with("gwiki-page") {
            output.push_str(&line[start..marker_start]);
            output.push_str("<! --");
            start = marker_start + "<!--".len();
        } else {
            output.push_str(&line[start..cursor]);
            start = cursor;
        }
    }
    output.push_str(&line[start..]);
    output
}

fn is_markdown_horizontal_rule(trimmed: &str) -> bool {
    let mut marker = None;
    let mut count = 0usize;
    for ch in trimmed.chars() {
        if ch.is_whitespace() {
            continue;
        }
        if !matches!(ch, '-' | '_' | '*') {
            return false;
        }
        match marker {
            None => marker = Some(ch),
            Some(expected) if expected == ch => {}
            Some(_) => return false,
        }
        count += 1;
    }
    count >= 3
}

pub(crate) fn merge_pdf_pages(
    snapshot: &PdfSnapshot,
    asset_path: &Path,
    rendered_pages: Vec<PdfRenderedPage>,
    endpoint: VisionEndpoint<'_>,
    degradations: Vec<DocumentDegradation>,
) -> (Vec<PdfPageMarkdown>, PdfMarkdownSummary) {
    let text_pages = snapshot
        .pages
        .iter()
        .map(|page| (page.number, page.text.as_str()))
        .collect::<BTreeMap<_, _>>();
    let rendered_page_map = rendered_pages
        .into_iter()
        .map(|page| (page.number, page))
        .collect::<BTreeMap<_, _>>();
    let page_numbers = text_pages
        .keys()
        .chain(rendered_page_map.keys())
        .copied()
        .collect::<BTreeSet<_>>();
    let mut vision_used = false;
    let mut models = BTreeSet::new();
    let mut pages = Vec::with_capacity(page_numbers.len());
    let mut vision_failed = false;

    for number in page_numbers {
        let text_layer = text_pages.get(&number).copied().unwrap_or_default();
        let vision = rendered_page_map.get(&number).and_then(|rendered| {
            match extract_vision_for_page(snapshot, asset_path, rendered, &endpoint) {
                Ok(vision) => vision,
                Err(_) => {
                    vision_failed = true;
                    None
                }
            }
        });
        if let Some(vision) = &vision {
            vision_used = true;
            if let Some(model) = vision_model(vision) {
                models.insert(model.to_string());
            }
        }
        pages.push(PdfPageMarkdown {
            number,
            markdown: merge_page_markdown(text_layer, vision.as_ref()),
        });
    }

    let has_text_layer = snapshot
        .pages
        .iter()
        .any(|page| !normalize_page_text(&page.text).is_empty());
    let rendered_page_count = rendered_page_map.len();
    let page_count = text_pages.len().max(rendered_page_count);
    let mut degradations = degradations;
    if matches!(endpoint, VisionEndpoint::Unavailable(_)) && rendered_page_count > 0 {
        degradations.push(DocumentDegradation::new(
            DocumentFailureMode::PdfVisionUnavailable,
            DocumentUnitCount::pages(rendered_page_count),
            "PDF vision extraction is unavailable; original asset is preserved.",
        ));
    }
    if vision_failed {
        degradations.push(DocumentDegradation::new(
            DocumentFailureMode::PdfVisionError,
            DocumentUnitCount::pages(page_count),
            "PDF page vision extraction failed; original asset is preserved.",
        ));
    }
    (
        pages,
        PdfMarkdownSummary {
            page_count,
            has_text_layer,
            vision_used,
            model: (!models.is_empty()).then(|| models.into_iter().collect::<Vec<_>>().join(", ")),
            degradations,
        },
    )
}

fn extract_vision_for_page(
    snapshot: &PdfSnapshot,
    asset_path: &Path,
    rendered: &PdfRenderedPage,
    endpoint: &VisionEndpoint<'_>,
) -> Result<Option<VisionExtraction>, WikiError> {
    let client = match endpoint {
        VisionEndpoint::Available(client) => *client,
        VisionEndpoint::Unavailable(_) => return Ok(None),
    };
    let file_name = rendered_page_file_name(&snapshot.file_name, rendered.number);
    let image_asset_path = rendered_page_asset_path(asset_path, &file_name);
    client
        .extract(&VisionRequest {
            file_name: &file_name,
            mime_type: Some(rendered.mime_type.as_str()),
            asset_path: &image_asset_path,
            bytes: &rendered.bytes,
            width: rendered.width,
            height: rendered.height,
        })
        .map(Some)
}

fn rendered_page_asset_path(asset_path: &Path, file_name: &str) -> PathBuf {
    asset_path
        .parent()
        .map(|parent| parent.join(file_name))
        .unwrap_or_else(|| PathBuf::from(file_name))
}

fn merge_page_markdown(text_layer: &str, vision: Option<&VisionExtraction>) -> String {
    let text = normalize_page_text(text_layer);
    let mut sections = Vec::new();
    if !text.is_empty() {
        sections.push(text.clone());
    }
    if let Some(vision) = vision {
        if let Some(ocr_text) = vision
            .ocr_text
            .as_deref()
            .and_then(|ocr_text| dedupe_ocr_text(&text, ocr_text))
        {
            sections.push(format!("### OCR Text\n\n{ocr_text}"));
        }
        let description = single_line(&vision.description);
        if !description.is_empty() {
            sections.push(format!("### Visual Description\n\n{description}"));
        }
    }
    sections.join("\n\n")
}

fn dedupe_ocr_text(text_layer: &str, ocr_text: &str) -> Option<String> {
    let ocr = normalize_page_text(ocr_text);
    if ocr.is_empty() {
        return None;
    }
    let text_key = overlap_key(text_layer);
    if text_key.is_empty() {
        return Some(ocr);
    }
    let ocr_key = overlap_key(&ocr);
    if text_key.contains(&ocr_key) {
        return None;
    }
    let retained = ocr
        .split("\n\n")
        .filter(|paragraph| {
            let key = overlap_key(paragraph);
            !key.is_empty() && !text_key.contains(&key)
        })
        .collect::<Vec<_>>();
    (!retained.is_empty()).then(|| retained.join("\n\n"))
}

fn overlap_key(value: &str) -> String {
    single_line(value)
        .chars()
        .filter(|character| !character.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}

fn vision_model(vision: &VisionExtraction) -> Option<&str> {
    vision.metadata.iter().find_map(|(key, value)| {
        (key == "model" && !value.trim().is_empty()).then_some(value.as_str())
    })
}

fn rendered_page_file_name(file_name: &str, page_number: usize) -> String {
    let stem = Path::new(file_name)
        .file_stem()
        .and_then(|stem| stem.to_str())
        .unwrap_or(file_name);
    format!("{stem}-page-{page_number}.png")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_markdown_neutralizes_marker_variants() {
        let sanitized = sanitize_pdf_page_markdown(
            "<!--gwiki-page: 1 -->\n<!--   gwiki-page: 2 -->\n<!-- GwIkI-PaGe: 3 -->",
        );

        assert_eq!(
            sanitized,
            "<! --gwiki-page: 1 -->\n<! --   gwiki-page: 2 -->\n<! -- GwIkI-PaGe: 3 -->"
        );
    }

    #[test]
    fn ocr_overlap_key_preserves_punctuation_collisions() {
        assert_ne!(overlap_key("A/B"), overlap_key("AB"));
        assert_eq!(
            dedupe_ocr_text("Account A/B", "Account AB"),
            Some("Account AB".to_string())
        );
        assert_eq!(dedupe_ocr_text("Account A/B", "Account A/B"), None);
    }
}
