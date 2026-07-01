#[cfg(feature = "documents")]
use chrono::TimeZone;
use chrono::{DateTime, Utc};

/// Default rasterization density for PDF page images sent to vision extraction.
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub const DEFAULT_PDF_RENDER_DPI: u16 = 150;

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub struct PdfPage {
    pub number: usize,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub struct PdfSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: DateTime<Utc>,
    pub bytes: Vec<u8>,
    pub pages: Vec<PdfPage>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub struct PdfFileSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: DateTime<Utc>,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub struct PdfRenderedPage {
    pub number: usize,
    pub bytes: Vec<u8>,
    pub mime_type: String,
    pub width: Option<u32>,
    pub height: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
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

#[cfg(feature = "documents")]
pub(crate) fn pdf_fetched_at(value: &str) -> Result<DateTime<Utc>, crate::WikiError> {
    let value = value.trim();
    if let Some(millis) = value.strip_prefix("unix-ms:") {
        let millis = millis
            .parse::<i64>()
            .map_err(|error| crate::WikiError::Config {
                detail: format!("invalid PDF fetch timestamp `{value}`: {error}"),
            })?;
        return Utc
            .timestamp_millis_opt(millis)
            .single()
            .ok_or_else(|| crate::WikiError::Config {
                detail: format!("PDF fetch timestamp `{value}` is out of range"),
            });
    }

    DateTime::parse_from_rfc3339(value)
        .map(|timestamp| timestamp.with_timezone(&Utc))
        .map_err(|error| crate::WikiError::Config {
            detail: format!("invalid PDF fetch timestamp `{value}`: {error}"),
        })
}
