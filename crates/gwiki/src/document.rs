use crate::ingest::single_line;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DocumentFailureMode {
    OfficeParseError,
    HtmlParseError,
    HtmlNoContent,
    PdfTextLayerError,
    PdfRenderError,
    PdfRenderBudgetExceeded,
    PdfVisionUnavailable,
    PdfVisionError,
    PdfNoExtractableContent,
}

impl DocumentFailureMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::OfficeParseError => "office_parse_error",
            Self::HtmlParseError => "html_parse_error",
            Self::HtmlNoContent => "html_no_content",
            Self::PdfTextLayerError => "pdf_text_layer_error",
            Self::PdfRenderError => "pdf_render_error",
            Self::PdfRenderBudgetExceeded => "pdf_render_budget_exceeded",
            Self::PdfVisionUnavailable => "pdf_vision_unavailable",
            Self::PdfVisionError => "pdf_vision_error",
            Self::PdfNoExtractableContent => "pdf_no_extractable_content",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DocumentUnitCount {
    key: &'static str,
    count: usize,
}

impl DocumentUnitCount {
    pub fn pages(count: usize) -> Self {
        Self {
            key: "page_count",
            count,
        }
    }

    pub fn sheets(count: usize) -> Self {
        Self {
            key: "sheet_count",
            count,
        }
    }

    pub fn slides(count: usize) -> Self {
        Self {
            key: "slide_count",
            count,
        }
    }

    pub fn key(self) -> &'static str {
        self.key
    }

    pub fn count(self) -> usize {
        self.count
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentDegradation {
    mode: DocumentFailureMode,
    pub unit_count: DocumentUnitCount,
    pub fallback: String,
}

impl DocumentDegradation {
    pub fn new(
        mode: DocumentFailureMode,
        unit_count: DocumentUnitCount,
        fallback: impl Into<String>,
    ) -> Self {
        Self {
            mode,
            unit_count,
            fallback: fallback.into(),
        }
    }

    pub fn reason(&self) -> &'static str {
        self.mode.as_str()
    }
}

pub struct DocumentDegradationMatrix;

impl DocumentDegradationMatrix {
    pub fn metadata(
        degradation: &DocumentDegradation,
        file_size_bytes: usize,
    ) -> Vec<(String, String)> {
        vec![
            ("file_size_bytes".to_string(), file_size_bytes.to_string()),
            (
                degradation.unit_count.key().to_string(),
                degradation.unit_count.count().to_string(),
            ),
            (
                "media_degradation".to_string(),
                degradation.reason().to_string(),
            ),
        ]
    }

    pub fn markdown_section(degradation: &DocumentDegradation) -> String {
        let mut markdown = String::from("## Document Parse Unavailable\n\n");
        markdown.push_str(&single_line(degradation.reason()));
        markdown.push_str(": ");
        markdown.push_str(&single_line(&degradation.fallback));
        markdown.push_str("\n\n");
        markdown
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn document_degradation_matrix() {
        let cases = [
            (
                DocumentFailureMode::OfficeParseError,
                DocumentUnitCount::sheets(0),
                "office_parse_error",
                "sheet_count",
            ),
            (
                DocumentFailureMode::HtmlParseError,
                DocumentUnitCount::pages(1),
                "html_parse_error",
                "page_count",
            ),
            (
                DocumentFailureMode::HtmlNoContent,
                DocumentUnitCount::pages(1),
                "html_no_content",
                "page_count",
            ),
            (
                DocumentFailureMode::PdfTextLayerError,
                DocumentUnitCount::pages(0),
                "pdf_text_layer_error",
                "page_count",
            ),
            (
                DocumentFailureMode::PdfRenderError,
                DocumentUnitCount::pages(0),
                "pdf_render_error",
                "page_count",
            ),
            (
                DocumentFailureMode::PdfRenderBudgetExceeded,
                DocumentUnitCount::pages(0),
                "pdf_render_budget_exceeded",
                "page_count",
            ),
            (
                DocumentFailureMode::PdfVisionUnavailable,
                DocumentUnitCount::pages(2),
                "pdf_vision_unavailable",
                "page_count",
            ),
            (
                DocumentFailureMode::PdfVisionError,
                DocumentUnitCount::pages(2),
                "pdf_vision_error",
                "page_count",
            ),
            (
                DocumentFailureMode::PdfNoExtractableContent,
                DocumentUnitCount::pages(0),
                "pdf_no_extractable_content",
                "page_count",
            ),
        ];

        for (mode, unit_count, expected_reason, expected_count_key) in cases {
            let degradation = DocumentDegradation::new(mode, unit_count, "raw asset preserved");
            assert_eq!(degradation.reason(), expected_reason);
            assert_eq!(degradation.unit_count.key(), expected_count_key);

            let metadata = DocumentDegradationMatrix::metadata(&degradation, 4096);
            assert!(metadata.contains(&("file_size_bytes".to_string(), "4096".to_string())));
            assert!(
                metadata.contains(&("media_degradation".to_string(), expected_reason.to_string()))
            );
            assert!(metadata.contains(&(
                expected_count_key.to_string(),
                degradation.unit_count.count().to_string()
            )));

            let markdown = DocumentDegradationMatrix::markdown_section(&degradation);
            assert!(markdown.contains("## Document Parse Unavailable"));
            assert!(markdown.contains(expected_reason));
            assert!(markdown.contains("raw asset preserved"));
        }
    }
}
