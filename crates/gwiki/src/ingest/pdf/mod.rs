//! PDF ingestion: text-layer extraction, page rendering, and vision-merged Markdown.

use crate::document::DocumentDegradation;

pub(crate) mod ingest;
mod markdown;
mod render;
pub(crate) mod types;

// ingest::file (gated on the documents feature) is the only cross-module consumer
// of these PDF entry points, so the re-exports are gated to match.
#[cfg(feature = "documents")]
pub(crate) use ingest::ingest_pdf_file_without_index;
#[cfg(feature = "documents")]
pub use types::{PdfFileSnapshot, PdfIngestOptions};

#[cfg(feature = "documents")]
use types::PdfRenderedPage;

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

#[cfg(test)]
mod tests;
