use std::path::Path;

use crate::WikiError;
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    single_line, write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

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

pub fn ingest_pages(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: PdfSnapshot,
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
    let markdown = render_pdf_markdown(&snapshot, &title, &record.content_hash, &asset_path);
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: Some(asset_path),
    })
}

fn render_pdf_markdown(
    snapshot: &PdfSnapshot,
    title: &str,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let mut markdown = markdown_metadata(&[
        ("source_kind", "pdf".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", path_to_string(asset_path)),
    ]);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");

    if snapshot.pages.is_empty() {
        markdown.push_str("No extractable page text.\n");
        return markdown;
    }

    for page in &snapshot.pages {
        markdown.push_str("<!-- gwiki-page: ");
        markdown.push_str(&page.number.to_string());
        markdown.push_str(" -->\n\n");
        markdown.push_str("## Page ");
        markdown.push_str(&page.number.to_string());
        markdown.push_str("\n\n");
        markdown.push_str(&normalize_page_text(&page.text));
        markdown.push_str("\n\n");
    }
    markdown
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
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;

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
    fn pdf_page_text_preserves_paragraph_breaks() {
        let text = normalize_page_text("First line\nwraps here.\n\nSecond paragraph.\n");

        assert_eq!(text, "First line wraps here.\n\nSecond paragraph.");
    }
}
