#[cfg(feature = "documents")]
use std::io::Cursor;

#[cfg(feature = "documents")]
use crate::WikiError;
#[cfg(feature = "documents")]
use crate::document::{DocumentDegradation, DocumentFailureMode, DocumentUnitCount};

#[cfg(feature = "documents")]
use pdfium_render::prelude::{PdfRenderConfig, Pdfium};

#[cfg(feature = "documents")]
use super::PdfRenderOutcome;
#[cfg(feature = "documents")]
use super::types::{PdfFileSnapshot, PdfPage, PdfRenderedPage};

#[cfg(feature = "documents")]
const MAX_RENDERED_PDF_PAGES: usize = 32;
#[cfg(any(feature = "documents", test))]
const MAX_RENDERED_PDF_TOTAL_BYTES: usize = 32 * 1024 * 1024;

#[cfg(feature = "documents")]
pub(crate) fn extract_text_layer_pages(bytes: &[u8]) -> Result<Vec<PdfPage>, WikiError> {
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
pub(crate) fn render_pdf_pages(
    snapshot: &PdfFileSnapshot,
    dpi: u16,
) -> Result<PdfRenderOutcome, WikiError> {
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
        let width = bitmap_dimension_to_u32("width", bitmap.width())?;
        let height = bitmap_dimension_to_u32("height", bitmap.height())?;
        let encoded = encode_png_rgba(width, height, &bitmap.as_rgba_bytes())?;
        let Some(next_total_rendered_bytes) =
            next_rendered_byte_total(total_rendered_bytes, encoded.len())
        else {
            budget_exceeded = true;
            break;
        };
        total_rendered_bytes = next_total_rendered_bytes;
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

#[cfg(any(feature = "documents", test))]
fn next_rendered_byte_total(current: usize, page_bytes: usize) -> Option<usize> {
    let next = current.checked_add(page_bytes)?;
    (next <= MAX_RENDERED_PDF_TOTAL_BYTES).then_some(next)
}

#[cfg(feature = "documents")]
pub(crate) fn pdf_render_budget_degradation(
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
fn bitmap_dimension_to_u32(name: &str, value: i32) -> Result<u32, WikiError> {
    if value <= 0 {
        return Err(pdfium_error(format!(
            "bitmap {name} must be positive, got {value}"
        )));
    }
    u32::try_from(value)
        .map_err(|_| pdfium_error(format!("bitmap {name} exceeds u32 range: {value}")))
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_byte_budget_rejects_overflow_before_updating_total() {
        assert_eq!(next_rendered_byte_total(usize::MAX, 1), None);
        assert_eq!(
            next_rendered_byte_total(MAX_RENDERED_PDF_TOTAL_BYTES - 1, 1),
            Some(MAX_RENDERED_PDF_TOTAL_BYTES)
        );
        assert_eq!(
            next_rendered_byte_total(MAX_RENDERED_PDF_TOTAL_BYTES, 1),
            None
        );
    }

    #[cfg(feature = "documents")]
    #[test]
    fn bitmap_dimensions_reject_non_positive_values_before_cast() {
        let err = bitmap_dimension_to_u32("width", -1).expect_err("negative rejected");

        assert!(
            err.to_string().contains("bitmap width must be positive"),
            "unexpected error: {err}"
        );
    }
}
