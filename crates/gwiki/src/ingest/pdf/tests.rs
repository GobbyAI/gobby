use std::path::Path;

use gobby_core::indexing::content_hash;

use super::ingest::{ingest_pages, ingest_pages_with_vision};
use super::markdown::{render_pdf_markdown, sanitize_pdf_page_markdown};
use super::render::normalize_page_text;
#[cfg(feature = "documents")]
use super::render::pdf_render_budget_degradation;
use super::types::{PdfPage, PdfRenderedPage, PdfSnapshot};
use super::*;
use crate::ScopeIdentity;
use crate::WikiError;
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

    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
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
fn pdf_markdown_escapes_horizontal_rules() {
    let sanitized = sanitize_pdf_page_markdown("---\n- - -\n___\n* * *\nnot ---");

    assert_eq!(sanitized, "\\---\n\\- - -\n\\___\n\\* * *\nnot ---");
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

    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
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
    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
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
    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
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
    let raw =
        std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown written");
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
