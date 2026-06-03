use std::collections::{BTreeMap, BTreeSet};
#[cfg(feature = "documents")]
use std::io::Cursor;
use std::path::{Path, PathBuf};

use crate::ScopeIdentity;
use crate::WikiError;
use crate::document::{DocumentDegradation, DocumentFailureMode, DocumentUnitCount};
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    single_line, write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::vision::{VisionEndpoint, VisionExtraction, VisionRequest};

#[cfg(feature = "documents")]
use pdfium_render::prelude::{PdfRenderConfig, Pdfium};

const DEFAULT_PDF_RENDER_DPI: u16 = 150;
const MAX_RENDERED_PDF_PAGES: usize = 32;
const MAX_RENDERED_PDF_TOTAL_BYTES: usize = 32 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PdfPage {
    pub number: usize,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PdfSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub pages: Vec<PdfPage>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PdfFileSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PdfRenderedPage {
    pub number: usize,
    pub bytes: Vec<u8>,
    pub mime_type: String,
    pub width: Option<u32>,
    pub height: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PdfIngestOptions {
    pub render_dpi: u16,
}

impl Default for PdfIngestOptions {
    fn default() -> Self {
        Self {
            render_dpi: DEFAULT_PDF_RENDER_DPI,
        }
    }
}

struct PdfPageMarkdown {
    number: usize,
    markdown: String,
}

struct PdfMarkdownSummary {
    page_count: usize,
    has_text_layer: bool,
    vision_used: bool,
    model: Option<String>,
    degradations: Vec<DocumentDegradation>,
}

#[cfg(feature = "documents")]
struct PdfRenderOutcome {
    pages: Vec<PdfRenderedPage>,
    degradation: Option<DocumentDegradation>,
}

pub fn ingest_pages(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: PdfSnapshot,
) -> Result<IngestResult, WikiError> {
    ingest_pages_with_vision(
        vault_root,
        store,
        &ScopeIdentity::global(),
        snapshot,
        Vec::new(),
        VisionEndpoint::Unavailable(crate::vision::VisionDegradation {
            reason: "disabled".to_string(),
            fallback: "Keep PDF text layer only.".to_string(),
        }),
    )
}

#[cfg(feature = "documents")]
pub fn ingest_pdf_file(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: &ScopeIdentity,
    snapshot: PdfFileSnapshot,
    endpoint: VisionEndpoint<'_>,
    options: PdfIngestOptions,
) -> Result<IngestResult, WikiError> {
    let result = ingest_pdf_file_without_index(vault_root, scope, snapshot, endpoint, options)?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

#[cfg(feature = "documents")]
pub(crate) fn ingest_pdf_file_without_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    snapshot: PdfFileSnapshot,
    endpoint: VisionEndpoint<'_>,
    options: PdfIngestOptions,
) -> Result<IngestResult, WikiError> {
    let mut degradations = Vec::new();
    let pages = match extract_text_layer_pages(&snapshot.bytes) {
        Ok(pages) => pages,
        Err(error) => {
            degradations.push(DocumentDegradation::new(
                DocumentFailureMode::PdfTextLayerError,
                DocumentUnitCount::pages(0),
                format!("PDF text-layer extraction failed: {error}; original asset is preserved."),
            ));
            Vec::new()
        }
    };
    let rendered_pages = match endpoint {
        VisionEndpoint::Available(_) => match render_pdf_pages(&snapshot, options.render_dpi) {
            Ok(outcome) => {
                if let Some(degradation) = outcome.degradation {
                    degradations.push(degradation);
                }
                outcome.pages
            }
            Err(error) => {
                degradations.push(DocumentDegradation::new(
                    DocumentFailureMode::PdfRenderError,
                    DocumentUnitCount::pages(pages.len()),
                    format!("PDF page rendering failed: {error}; original asset is preserved."),
                ));
                Vec::new()
            }
        },
        VisionEndpoint::Unavailable(_) => Vec::new(),
    };

    ingest_pages_with_vision_inner(
        vault_root,
        scope,
        PdfSnapshot {
            location: snapshot.location,
            file_name: snapshot.file_name,
            fetched_at: snapshot.fetched_at,
            bytes: snapshot.bytes,
            pages,
        },
        rendered_pages,
        endpoint,
        degradations,
    )
}

pub fn ingest_pages_with_vision(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: &ScopeIdentity,
    snapshot: PdfSnapshot,
    rendered_pages: Vec<PdfRenderedPage>,
    endpoint: VisionEndpoint<'_>,
) -> Result<IngestResult, WikiError> {
    let result = ingest_pages_with_vision_without_index(
        vault_root,
        scope,
        snapshot,
        rendered_pages,
        endpoint,
    )?;
    index_after_ingest(vault_root, store)?;
    Ok(result)
}

pub(crate) fn ingest_pages_with_vision_without_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    snapshot: PdfSnapshot,
    rendered_pages: Vec<PdfRenderedPage>,
    endpoint: VisionEndpoint<'_>,
) -> Result<IngestResult, WikiError> {
    ingest_pages_with_vision_inner(
        vault_root,
        scope,
        snapshot,
        rendered_pages,
        endpoint,
        Vec::new(),
    )
}

fn ingest_pages_with_vision_inner(
    vault_root: &Path,
    scope: &ScopeIdentity,
    snapshot: PdfSnapshot,
    rendered_pages: Vec<PdfRenderedPage>,
    endpoint: VisionEndpoint<'_>,
    mut degradations: Vec<DocumentDegradation>,
) -> Result<IngestResult, WikiError> {
    let title = markdown_title(&snapshot.file_name);
    let draft = SourceDraft {
        location: snapshot.location.clone(),
        kind: SourceKind::Pdf,
        fetched_at: snapshot.fetched_at.clone(),
        content: snapshot.bytes.clone(),
        title: Some(title.clone()),
        citation: Some(snapshot.location.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    if snapshot
        .pages
        .iter()
        .all(|page| normalize_page_text(&page.text).is_empty())
        && rendered_pages.is_empty()
    {
        degradations.push(DocumentDegradation::new(
            DocumentFailureMode::PdfNoExtractableContent,
            DocumentUnitCount::pages(snapshot.pages.len()),
            "PDF contained no extractable text and no usable rendered page vision; original asset is preserved.",
        ));
    }
    let (pages, summary) = merge_pdf_pages(
        &snapshot,
        &asset_path,
        rendered_pages,
        endpoint,
        degradations,
    );
    let markdown = render_pdf_markdown(
        scope,
        &snapshot,
        &title,
        &record.content_hash,
        &asset_path,
        &pages,
        &summary,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: Some(asset_path),
    })
}

fn render_pdf_markdown(
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
        ("fetched_at", snapshot.fetched_at.clone()),
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

fn sanitize_pdf_page_markdown(markdown: &str) -> String {
    markdown
        .lines()
        .map(|line| {
            let trimmed = line.trim();
            let line = if trimmed.len() >= 3 && trimmed.bytes().all(|byte| byte == b'-') {
                format!("\\{line}")
            } else {
                line.to_string()
            };
            line.replace("<!-- gwiki-page:", "<!-- gwiki-page :")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn merge_pdf_pages(
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
        .filter(|character| character.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn vision_model(vision: &VisionExtraction) -> Option<&str> {
    vision.metadata.iter().find_map(|(key, value)| {
        (key == "model" && !value.trim().is_empty()).then_some(value.as_str())
    })
}

fn rendered_page_file_name(file_name: &str, page_number: usize) -> String {
    let stem = file_name.strip_suffix(".pdf").unwrap_or(file_name);
    format!("{stem}-page-{page_number}.png")
}

#[cfg(feature = "documents")]
fn extract_text_layer_pages(bytes: &[u8]) -> Result<Vec<PdfPage>, WikiError> {
    pdf_extract::extract_text_from_mem_by_pages(bytes)
        .map(|pages| {
            pages
                .into_iter()
                .enumerate()
                .map(|(index, text)| PdfPage {
                    number: index + 1,
                    text,
                })
                .collect()
        })
        .map_err(|error| WikiError::InvalidInput {
            field: "pdf",
            message: format!("failed to extract PDF text layer: {error}"),
        })
}

#[cfg(feature = "documents")]
fn render_pdf_pages(snapshot: &PdfFileSnapshot, dpi: u16) -> Result<PdfRenderOutcome, WikiError> {
    let pdfium = bundled_pdfium()?;
    let document = pdfium
        .load_pdf_from_byte_slice(&snapshot.bytes, None)
        .map_err(pdfium_error)?;
    let render_dpi = dpi.max(1);
    let total_pages = document.pages().len() as usize;
    let mut rendered_pages = Vec::with_capacity(total_pages.min(MAX_RENDERED_PDF_PAGES));
    let mut total_rendered_bytes = 0usize;
    let mut budget_exceeded = false;

    for (index, page) in document.pages().iter().enumerate() {
        if index >= MAX_RENDERED_PDF_PAGES {
            budget_exceeded = true;
            break;
        }
        let target_width = points_to_pixels(page.width().value, render_dpi);
        let bitmap = page
            .render_with_config(
                &PdfRenderConfig::new()
                    .set_target_width(target_width)
                    .render_form_data(true)
                    .render_annotations(true),
            )
            .map_err(pdfium_error)?;
        let width = bitmap.width() as u32;
        let height = bitmap.height() as u32;
        let encoded = encode_png_rgba(width, height, &bitmap.as_rgba_bytes())?;
        if total_rendered_bytes.saturating_add(encoded.len()) > MAX_RENDERED_PDF_TOTAL_BYTES {
            budget_exceeded = true;
            break;
        }
        total_rendered_bytes += encoded.len();
        rendered_pages.push(PdfRenderedPage {
            number: index + 1,
            bytes: encoded,
            mime_type: "image/png".to_string(),
            width: Some(width),
            height: Some(height),
        });
    }

    Ok(PdfRenderOutcome {
        pages: rendered_pages,
        degradation: budget_exceeded
            .then(|| pdf_render_budget_degradation(total_pages, total_rendered_bytes)),
    })
}

#[cfg(feature = "documents")]
fn pdf_render_budget_degradation(
    total_pages: usize,
    total_rendered_bytes: usize,
) -> DocumentDegradation {
    DocumentDegradation::new(
        DocumentFailureMode::PdfRenderBudgetExceeded,
        DocumentUnitCount::pages(total_pages),
        format!(
            "PDF page rendering stopped after the configured budget of {MAX_RENDERED_PDF_PAGES} page(s) or {MAX_RENDERED_PDF_TOTAL_BYTES} byte(s); {total_rendered_bytes} rendered byte(s) were retained and the original asset is preserved.",
        ),
    )
}

#[cfg(feature = "documents")]
fn bundled_pdfium() -> Result<Pdfium, WikiError> {
    let path = pdfium_auto::ensure_pdfium_bundled().map_err(|error| WikiError::InvalidInput {
        field: "pdf",
        message: format!("failed to initialize bundled pdfium: {error}"),
    })?;
    Pdfium::bind_to_library(&path)
        .map(Pdfium::new)
        .map_err(|error| WikiError::InvalidInput {
            field: "pdf",
            message: format!("failed to initialize bundled pdfium: {error}"),
        })
}

#[cfg(feature = "documents")]
fn points_to_pixels(points: f32, dpi: u16) -> i32 {
    ((points / 72.0) * f32::from(dpi)).round().max(1.0) as i32
}

#[cfg(feature = "documents")]
fn encode_png_rgba(width: u32, height: u32, rgba: &[u8]) -> Result<Vec<u8>, WikiError> {
    let mut encoded = Cursor::new(Vec::new());
    let mut encoder = png::Encoder::new(&mut encoded, width, height);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder
        .write_header()
        .map_err(|error| WikiError::InvalidInput {
            field: "pdf",
            message: format!("failed to encode rendered PDF page: {error}"),
        })?;
    writer
        .write_image_data(rgba)
        .map_err(|error| WikiError::InvalidInput {
            field: "pdf",
            message: format!("failed to encode rendered PDF page: {error}"),
        })?;
    drop(writer);
    Ok(encoded.into_inner())
}

#[cfg(feature = "documents")]
fn pdfium_error(error: impl std::fmt::Display) -> WikiError {
    WikiError::InvalidInput {
        field: "pdf",
        message: format!("failed to render PDF page: {error}"),
    }
}

fn normalize_page_text(text: &str) -> String {
    let mut paragraphs = Vec::new();
    let mut current = Vec::new();

    for line in text.lines() {
        let line = single_line(line);
        if line.is_empty() {
            if !current.is_empty() {
                paragraphs.push(current.join(" "));
                current.clear();
            }
            continue;
        }
        current.push(line);
    }

    if !current.is_empty() {
        paragraphs.push(current.join(" "));
    }

    paragraphs.join("\n\n")
}

#[cfg(test)]
mod tests {
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::ScopeIdentity;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;
    use crate::vision::{VisionClient, VisionEndpoint, VisionExtraction, VisionRequest};

    struct FakePdfVisionClient;

    impl VisionClient for FakePdfVisionClient {
        fn extract(&self, request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError> {
            assert_eq!(request.mime_type, Some("image/png"));
            assert_eq!(request.width, Some(1200));
            assert_eq!(request.height, Some(1600));
            assert_eq!(
                request
                    .asset_path
                    .file_name()
                    .and_then(|name| name.to_str()),
                Some(request.file_name)
            );
            assert_eq!(
                request
                    .asset_path
                    .extension()
                    .and_then(|name| name.to_str()),
                Some("png")
            );
            let page_2 = request.file_name.contains("page-2");

            Ok(VisionExtraction {
                description: format!("Visual description for {}.", request.file_name),
                ocr_text: Some(if page_2 {
                    "Scanned invoice total: $42".to_string()
                } else {
                    "First page fact.\n\nChart label: Growth".to_string()
                }),
                metadata: vec![("model".to_string(), "vision-test".to_string())],
            })
        }
    }

    #[test]
    fn combines_text_layer_and_vision() {
        let temp = tempfile::tempdir().expect("tempdir");
        let bytes = b"%PDF-1.7\nsource bytes\n%%EOF\n".to_vec();
        let snapshot = PdfSnapshot {
            location: "/tmp/guide.pdf".to_string(),
            file_name: "guide.pdf".to_string(),
            fetched_at: "2026-05-29T16:30:00Z".to_string(),
            bytes,
            pages: vec![
                PdfPage {
                    number: 1,
                    text: "First page fact.".to_string(),
                },
                PdfPage {
                    number: 2,
                    text: String::new(),
                },
            ],
        };
        let rendered_pages = vec![
            PdfRenderedPage {
                number: 1,
                bytes: b"rendered-png-page-1".to_vec(),
                mime_type: "image/png".to_string(),
                width: Some(1200),
                height: Some(1600),
            },
            PdfRenderedPage {
                number: 2,
                bytes: b"rendered-png-page-2".to_vec(),
                mime_type: "image/png".to_string(),
                width: Some(1200),
                height: Some(1600),
            },
        ];
        let vision = FakePdfVisionClient;
        let mut store = MemoryWikiStore::default();

        let result = ingest_pages_with_vision(
            temp.path(),
            &mut store,
            &ScopeIdentity::global(),
            snapshot,
            rendered_pages,
            VisionEndpoint::Available(&vision),
        )
        .expect("ingest combined pdf");

        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("page_count: 2"));
        assert!(raw.contains("has_text_layer: true"));
        assert!(raw.contains("vision_used: true"));
        assert!(raw.contains("model: vision-test"));
        assert!(raw.contains("First page fact."));
        assert!(raw.contains("Chart label: Growth"));
        assert!(raw.contains("Scanned invoice total: $42"));
        assert_eq!(raw.matches("First page fact.").count(), 1);
        assert!(raw.contains("Visual description for guide-page-1.png."));
        assert!(raw.contains("Visual description for guide-page-2.png."));
    }

    #[test]
    fn pdf_ingest_preserves_page_refs() {
        let temp = tempfile::tempdir().expect("tempdir");
        let bytes = b"%PDF-1.7\nsource bytes\n%%EOF\n".to_vec();
        let expected_hash = content_hash(&bytes);
        let snapshot = PdfSnapshot {
            location: "/tmp/guide.pdf".to_string(),
            file_name: "guide.pdf".to_string(),
            fetched_at: "2026-05-29T16:30:00Z".to_string(),
            bytes: bytes.clone(),
            pages: vec![
                PdfPage {
                    number: 1,
                    text: "First page fact.".to_string(),
                },
                PdfPage {
                    number: 2,
                    text: "Second page citation.".to_string(),
                },
            ],
        };
        let mut store = MemoryWikiStore::default();

        let result = ingest_pages(temp.path(), &mut store, snapshot).expect("ingest pdf");

        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("# guide.pdf"));
        assert!(raw.contains("source_hash: "));
        assert!(raw.contains("<!-- gwiki-page: 1 -->"));
        assert!(raw.contains("## Page 1"));
        assert!(raw.contains("First page fact."));
        assert!(raw.contains("<!-- gwiki-page: 2 -->"));
        assert!(raw.contains("## Page 2"));
        assert!(raw.contains("Second page citation."));

        let asset_path = result.asset_path.expect("pdf asset path");
        assert_eq!(
            std::fs::read(temp.path().join(asset_path)).expect("asset bytes"),
            bytes
        );

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].kind, SourceKind::Pdf);
        assert_eq!(manifest.entries[0].content_hash, expected_hash);
    }

    #[test]
    fn pdf_page_body_sanitizes_internal_markers_and_fences() {
        let snapshot = PdfSnapshot {
            location: "/tmp/report.pdf".to_string(),
            file_name: "report.pdf".to_string(),
            fetched_at: "2026-05-29T21:30:00Z".to_string(),
            bytes: vec![0; 10],
            pages: Vec::new(),
        };
        let markdown = render_pdf_markdown(
            &ScopeIdentity::global(),
            &snapshot,
            "report.pdf",
            "hash",
            Path::new("raw/assets/report.pdf"),
            &[PdfPageMarkdown {
                number: 1,
                markdown: "before\n<!-- gwiki-page: 99 -->\n---\n----\nafter".to_string(),
            }],
            &PdfMarkdownSummary {
                page_count: 1,
                has_text_layer: true,
                vision_used: false,
                model: None,
                degradations: Vec::new(),
            },
        );

        assert!(markdown.contains("<!-- gwiki-page: 1 -->"));
        assert!(!markdown.contains("<!-- gwiki-page: 99 -->"));
        assert!(markdown.contains("<!-- gwiki-page : 99 -->"));
        assert!(markdown.contains("\n\\---\n"));
        assert!(markdown.contains("\n\\----\n"));
    }

    #[test]
    fn pdf_page_text_preserves_paragraph_breaks() {
        let text = normalize_page_text("First line\nwraps here.\n\nSecond paragraph.\n");

        assert_eq!(text, "First line wraps here.\n\nSecond paragraph.");
    }

    #[test]
    fn pdf_degradation_uses_uniform_metadata() {
        struct FailingPdfVisionClient;

        impl VisionClient for FailingPdfVisionClient {
            fn extract(&self, _request: &VisionRequest<'_>) -> Result<VisionExtraction, WikiError> {
                Err(WikiError::InvalidInput {
                    field: "vision",
                    message: "vision provider failed".to_string(),
                })
            }
        }

        let temp = tempfile::tempdir().expect("tempdir");
        let bytes = b"%PDF-1.7\nsource bytes\n%%EOF\n".to_vec();
        let mut store = MemoryWikiStore::default();
        let vision = FailingPdfVisionClient;
        let snapshot = PdfSnapshot {
            location: "/tmp/scanned.pdf".to_string(),
            file_name: "scanned.pdf".to_string(),
            fetched_at: "2026-05-29T16:30:00Z".to_string(),
            bytes: bytes.clone(),
            pages: vec![PdfPage {
                number: 1,
                text: String::new(),
            }],
        };
        let rendered_pages = vec![PdfRenderedPage {
            number: 1,
            bytes: b"rendered-png-page-1".to_vec(),
            mime_type: "image/png".to_string(),
            width: Some(1200),
            height: Some(1600),
        }];

        let result = ingest_pages_with_vision(
            temp.path(),
            &mut store,
            &ScopeIdentity::global(),
            snapshot,
            rendered_pages,
            VisionEndpoint::Available(&vision),
        )
        .expect("ingest degraded pdf");
        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("media_degradation: pdf_vision_error"));
        assert!(raw.contains("## Document Degradation"));
        assert!(raw.contains("pdf_vision_error"));
        assert!(raw.contains("file_size_bytes: 28"));
        assert!(raw.contains("page_count: 1"));
        assert!(raw.contains("No extractable page text."));

        let asset_path = result.asset_path.expect("pdf asset path");
        assert_eq!(
            std::fs::read(temp.path().join(asset_path)).expect("pdf asset"),
            bytes
        );

        let text_only_pdf = PdfSnapshot {
            location: "/tmp/text-only.pdf".to_string(),
            file_name: "text-only.pdf".to_string(),
            fetched_at: "2026-05-29T16:30:00Z".to_string(),
            bytes: b"%PDF text".to_vec(),
            pages: vec![PdfPage {
                number: 1,
                text: "Text layer only.".to_string(),
            }],
        };
        let result = ingest_pages_with_vision(
            temp.path(),
            &mut store,
            &ScopeIdentity::global(),
            text_only_pdf,
            Vec::new(),
            VisionEndpoint::Unavailable(crate::vision::VisionDegradation {
                reason: "disabled".to_string(),
                fallback: "vision disabled".to_string(),
            }),
        )
        .expect("ingest text-only pdf");
        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(!raw.contains("media_degradation: pdf_vision_unavailable"));

        let empty_pdf = PdfSnapshot {
            location: "/tmp/empty.pdf".to_string(),
            file_name: "empty.pdf".to_string(),
            fetched_at: "2026-05-29T16:30:00Z".to_string(),
            bytes: b"%PDF empty".to_vec(),
            pages: Vec::new(),
        };
        let result = ingest_pages_with_vision(
            temp.path(),
            &mut store,
            &ScopeIdentity::global(),
            empty_pdf,
            Vec::new(),
            VisionEndpoint::Unavailable(crate::vision::VisionDegradation {
                reason: "disabled".to_string(),
                fallback: "vision disabled".to_string(),
            }),
        )
        .expect("ingest empty degraded pdf");
        let raw = std::fs::read_to_string(temp.path().join(&result.raw_path))
            .expect("raw markdown written");
        assert!(raw.contains("media_degradation: pdf_no_extractable_content"));
        assert!(raw.contains("file_size_bytes: 10"));
        assert!(raw.contains("page_count: 0"));
    }

    #[cfg(feature = "documents")]
    #[test]
    fn pdf_render_budget_degradation_records_limits() {
        let degradation = pdf_render_budget_degradation(40, 1024);

        assert_eq!(degradation.reason(), "pdf_render_budget_exceeded");
        assert_eq!(degradation.unit_count.count(), 40);
        assert!(degradation.fallback.contains("32 page(s)"));
        assert!(degradation.fallback.contains("1024 rendered byte(s)"));
    }
}
