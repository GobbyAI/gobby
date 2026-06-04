const DEFAULT_PDF_RENDER_DPI: u16 = 150;

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
