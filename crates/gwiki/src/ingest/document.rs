use std::io::{Cursor, Read};
use std::path::{Path, PathBuf};

use calamine::{Data, Reader, open_workbook_auto_from_rs};
use quick_xml::Reader as XmlReader;
use quick_xml::events::Event;
use scraper::{ElementRef, Html, Node, Selector};
use zip::ZipArchive;

use crate::document::{
    DocumentDegradation, DocumentDegradationMatrix, DocumentFailureMode, DocumentUnitCount,
};
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    single_line, text_from_utf8_lossy, write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraftRef, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::{ScopeIdentity, WikiError};

/// Maximum spreadsheet sheets rendered into markdown during bounded extraction.
const MAX_SHEETS: usize = 8;
/// Maximum rows rendered per spreadsheet sheet before truncation is reported.
const MAX_ROWS_PER_SHEET: usize = 64;
/// Maximum columns rendered per spreadsheet sheet before truncation is reported.
const MAX_COLUMNS_PER_SHEET: usize = 16;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub kind: SourceKind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentIngestResult {
    pub record: crate::sources::SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: PathBuf,
    pub derived_path: PathBuf,
    pub document_degradation: Option<DocumentDegradation>,
}

impl From<DocumentIngestResult> for IngestResult {
    fn from(result: DocumentIngestResult) -> Self {
        Self {
            record: result.record,
            raw_path: result.raw_path,
            asset_path: Some(result.asset_path),
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct DocumentRequest<'a> {
    pub file_name: &'a str,
    pub kind: &'a SourceKind,
    pub bytes: &'a [u8],
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentExtraction {
    pub title: Option<String>,
    pub markdown: String,
    pub units_label: &'static str,
    pub units_count: usize,
}

pub trait DocumentExtractor {
    fn extract(&self, request: &DocumentRequest<'_>) -> Result<DocumentExtraction, WikiError>;
}

pub enum DocumentEndpoint<'a> {
    Available(&'a dyn DocumentExtractor),
    Unavailable(DocumentDegradation),
}

struct LocalDocumentExtractor;

pub fn ingest_document(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: DocumentSnapshot,
) -> Result<DocumentIngestResult, WikiError> {
    static EXTRACTOR: LocalDocumentExtractor = LocalDocumentExtractor;
    ingest_document_with_endpoint(
        vault_root,
        store,
        scope,
        snapshot,
        DocumentEndpoint::Available(&EXTRACTOR),
    )
}

pub fn ingest_document_with_endpoint(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: DocumentSnapshot,
    endpoint: DocumentEndpoint<'_>,
) -> Result<DocumentIngestResult, WikiError> {
    let request = DocumentRequest {
        file_name: &snapshot.file_name,
        kind: &snapshot.kind,
        bytes: &snapshot.bytes,
    };
    let (extraction, degradation) = match endpoint {
        DocumentEndpoint::Available(extractor) => match extractor.extract(&request) {
            Ok(extraction) => (Some(extraction), None),
            Err(error) => (
                None,
                Some(document_degradation_for_error(&request, error.to_string())),
            ),
        },
        DocumentEndpoint::Unavailable(degradation) => (None, Some(degradation)),
    };
    let title = extraction
        .as_ref()
        .and_then(|value| value.title.clone())
        .unwrap_or_else(|| markdown_title(&snapshot.file_name));
    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: snapshot.location.clone(),
            kind: snapshot.kind.clone(),
            fetched_at: snapshot.fetched_at.clone(),
            content: &snapshot.bytes,
            title: Some(title.clone()),
            citation: Some(snapshot.location.clone()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    let raw_markdown = render_raw_document_markdown(&snapshot, &record.content_hash, &asset_path);
    let raw_path = write_raw_markdown(vault_root, &record, &raw_markdown)?;
    let derived_path = write_document_derived_markdown(
        vault_root,
        &scope,
        &record,
        &snapshot,
        &title,
        &asset_path,
        extraction.as_ref(),
        degradation.as_ref(),
    )?;
    index_after_ingest(vault_root, store)?;

    Ok(DocumentIngestResult {
        record,
        raw_path,
        asset_path,
        derived_path,
        document_degradation: degradation,
    })
}

impl DocumentExtractor for LocalDocumentExtractor {
    fn extract(&self, request: &DocumentRequest<'_>) -> Result<DocumentExtraction, WikiError> {
        match request.kind {
            SourceKind::Html => extract_html_document(request.bytes),
            SourceKind::Office => extract_office_document(request.file_name, request.bytes),
            _ => Err(document_error("unsupported document kind")),
        }
    }
}

fn extract_office_document(file_name: &str, bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
    match extension(file_name).as_deref() {
        Some("docx") => extract_docx(bytes),
        Some("pptx") => extract_pptx(bytes),
        Some("xlsx" | "xls" | "ods") => extract_spreadsheet(bytes),
        Some(extension) => Err(document_error(format!(
            "unsupported office extension .{extension}"
        ))),
        None => Err(document_error("missing office extension")),
    }
}

fn extract_docx(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
    let xml = read_zip_entry(bytes, "word/document.xml")?;
    let paragraphs = extract_xml_paragraphs(&xml)?;
    if paragraphs.is_empty() {
        return Err(document_error("docx contained no paragraph text"));
    }
    Ok(DocumentExtraction {
        title: paragraphs.first().cloned(),
        markdown: paragraphs.join("\n\n"),
        units_label: "paragraph_count",
        units_count: paragraphs.len(),
    })
}

fn extract_pptx(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
    let mut archive = zip_archive(bytes)?;
    let mut slide_names = archive
        .file_names()
        .filter(|name| name.starts_with("ppt/slides/slide") && name.ends_with(".xml"))
        .map(str::to_string)
        .collect::<Vec<_>>();
    slide_names.sort_by_key(|name| slide_number(name).unwrap_or(usize::MAX));
    if slide_names.is_empty() {
        return Err(document_error("pptx contained no slide XML"));
    }

    let mut markdown = String::new();
    let mut title = None;
    let mut slide_count = 0;
    for (index, name) in slide_names.iter().enumerate() {
        let mut xml = String::new();
        archive
            .by_name(name)
            .map_err(|error| document_error(format!("read {name}: {error}")))?
            .read_to_string(&mut xml)
            .map_err(|error| document_error(format!("read {name}: {error}")))?;
        let paragraphs = extract_xml_paragraphs(&xml)?;
        if paragraphs.is_empty() {
            continue;
        }
        slide_count += 1;
        if title.is_none() {
            title = paragraphs.first().cloned();
        }
        if !markdown.is_empty() {
            markdown.push('\n');
        }
        markdown.push_str("## Slide ");
        markdown.push_str(&(index + 1).to_string());
        markdown.push_str("\n\n");
        markdown.push_str(&paragraphs.join("\n\n"));
        markdown.push_str("\n\n");
    }
    if markdown.trim().is_empty() {
        return Err(document_error("pptx contained no slide text"));
    }
    Ok(DocumentExtraction {
        title,
        markdown: markdown.trim_end().to_string(),
        units_label: "slide_count",
        units_count: slide_count,
    })
}

fn extract_spreadsheet(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
    let cursor = Cursor::new(bytes.to_vec());
    let mut workbook = open_workbook_auto_from_rs(cursor)
        .map_err(|error| document_error(format!("open spreadsheet: {error}")))?;
    let sheet_names = workbook.sheet_names().to_vec();
    if sheet_names.is_empty() {
        return Err(document_error("spreadsheet contained no sheets"));
    }

    let mut markdown = String::new();
    let mut rendered_sheets = 0;
    for sheet_name in sheet_names.iter().take(MAX_SHEETS) {
        let range = workbook
            .worksheet_range(sheet_name)
            .map_err(|error| document_error(format!("read sheet {sheet_name}: {error}")))?;
        let rows = range
            .rows()
            .take(MAX_ROWS_PER_SHEET)
            .map(|row| {
                row.iter()
                    .take(MAX_COLUMNS_PER_SHEET)
                    .map(cell_text)
                    .collect::<Vec<_>>()
            })
            .filter(|row| row.iter().any(|cell| !cell.is_empty()))
            .collect::<Vec<_>>();
        if rows.is_empty() {
            continue;
        }
        rendered_sheets += 1;
        if !markdown.is_empty() {
            markdown.push('\n');
        }
        markdown.push_str("## Sheet: ");
        markdown.push_str(&single_line(sheet_name));
        markdown.push_str("\n\n");
        markdown.push_str(&markdown_table(&rows));
        markdown.push('\n');
        if range.height() > MAX_ROWS_PER_SHEET || range.width() > MAX_COLUMNS_PER_SHEET {
            markdown.push_str("\n_Table truncated for bounded extraction._\n");
        }
    }
    if markdown.trim().is_empty() {
        return Err(document_error("spreadsheet contained no cell text"));
    }
    Ok(DocumentExtraction {
        title: sheet_names.first().map(|name| markdown_title(name)),
        markdown: markdown.trim_end().to_string(),
        units_label: "sheet_count",
        units_count: rendered_sheets,
    })
}

fn extract_html_document(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
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
        return Err(document_error("html contained no readable text"));
    }
    Ok(DocumentExtraction {
        title,
        markdown,
        units_label: "section_count",
        units_count: 1,
    })
}

fn render_raw_document_markdown(
    snapshot: &DocumentSnapshot,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let fields = vec![
        ("source_kind", snapshot.kind.to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(&snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original document stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

#[allow(clippy::too_many_arguments)]
fn write_document_derived_markdown(
    vault_root: &Path,
    scope: &ScopeIdentity,
    record: &crate::sources::SourceRecord,
    snapshot: &DocumentSnapshot,
    title: &str,
    asset_path: &Path,
    extraction: Option<&DocumentExtraction>,
    degradation: Option<&DocumentDegradation>,
) -> Result<PathBuf, WikiError> {
    let relative_path = derived_markdown_path(record);
    let path = vault_root.join(&relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create document derived markdown directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let markdown = render_document_derived_markdown(
        scope,
        record,
        snapshot,
        title,
        asset_path,
        extraction,
        degradation,
    );
    std::fs::write(&path, markdown).map_err(|error| WikiError::Io {
        action: "write document derived markdown",
        path: Some(path),
        source: error,
    })?;
    Ok(relative_path)
}

fn render_document_derived_markdown(
    scope: &ScopeIdentity,
    record: &crate::sources::SourceRecord,
    snapshot: &DocumentSnapshot,
    title: &str,
    asset_path: &Path,
    extraction: Option<&DocumentExtraction>,
    degradation: Option<&DocumentDegradation>,
) -> String {
    let asset_path = path_to_string(asset_path);
    let raw_path = format!("raw/{}.md", record.id);
    let mut fields = vec![
        ("title".to_string(), title.to_string()),
        ("source_kind".to_string(), snapshot.kind.to_string()),
        ("source_location".to_string(), record.location.clone()),
        ("source_hash".to_string(), record.content_hash.clone()),
        ("source_asset".to_string(), asset_path.clone()),
        ("source_raw".to_string(), raw_path.clone()),
        ("fetched_at".to_string(), record.fetched_at.clone()),
        ("scope_kind".to_string(), scope.kind.as_str().to_string()),
        ("scope_id".to_string(), scope.id.clone()),
        (
            "document_status".to_string(),
            if extraction.is_some() {
                "extracted".to_string()
            } else {
                "unavailable".to_string()
            },
        ),
    ];
    if let Some(extraction) = extraction {
        fields.push((
            extraction.units_label.to_string(),
            extraction.units_count.to_string(),
        ));
    }
    if let Some(degradation) = degradation {
        fields.extend(DocumentDegradationMatrix::metadata(
            degradation,
            snapshot.bytes.len(),
        ));
    } else {
        fields.push((
            "file_size_bytes".to_string(),
            snapshot.bytes.len().to_string(),
        ));
    }

    let mut markdown = {
        let field_refs = fields
            .iter()
            .map(|(key, value)| (key.as_str(), value.clone()))
            .collect::<Vec<_>>();
        markdown_metadata(&field_refs)
    };
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    markdown.push_str("Original document: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n\n");
    markdown.push_str("Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n\n");

    if let Some(extraction) = extraction {
        markdown.push_str(&extraction.markdown);
        if !markdown.ends_with('\n') {
            markdown.push('\n');
        }
    } else if let Some(degradation) = degradation {
        markdown.push_str(&DocumentDegradationMatrix::markdown_section(degradation));
    }

    markdown.push_str("## Source References\n\n");
    markdown.push_str("- Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n");
    markdown.push_str("- Original document: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n");
    if let Some(citation) = &record.citation {
        markdown.push_str("- Citation: ");
        markdown.push_str(&single_line(citation));
        markdown.push('\n');
    }
    markdown
}

fn derived_markdown_path(record: &crate::sources::SourceRecord) -> PathBuf {
    PathBuf::from("wiki")
        .join("sources")
        .join(format!("{}.md", record.id))
}

fn document_degradation_for_error(
    request: &DocumentRequest<'_>,
    error: String,
) -> DocumentDegradation {
    let mode = match request.kind {
        SourceKind::Html => DocumentFailureMode::HtmlParseError,
        SourceKind::Office => DocumentFailureMode::OfficeParseError,
        _ => DocumentFailureMode::OfficeParseError,
    };
    DocumentDegradation::new(
        mode,
        document_unit_count_for_failure(request.file_name, request.kind),
        format!("Document parsing failed: {error}; original asset is preserved."),
    )
}

fn document_unit_count_for_failure(file_name: &str, kind: &SourceKind) -> DocumentUnitCount {
    match kind {
        SourceKind::Html => DocumentUnitCount::pages(1),
        SourceKind::Office => match extension(file_name).as_deref() {
            Some("pptx") => DocumentUnitCount::slides(0),
            Some("xlsx" | "xls" | "ods") => DocumentUnitCount::sheets(0),
            _ => DocumentUnitCount::pages(0),
        },
        _ => DocumentUnitCount::pages(0),
    }
}

fn read_zip_entry(bytes: &[u8], name: &str) -> Result<String, WikiError> {
    let mut archive = zip_archive(bytes)?;
    let mut xml = String::new();
    archive
        .by_name(name)
        .map_err(|error| document_error(format!("read {name}: {error}")))?
        .read_to_string(&mut xml)
        .map_err(|error| document_error(format!("read {name}: {error}")))?;
    Ok(xml)
}

fn zip_archive(bytes: &[u8]) -> Result<ZipArchive<Cursor<&[u8]>>, WikiError> {
    ZipArchive::new(Cursor::new(bytes))
        .map_err(|error| document_error(format!("open zip: {error}")))
}

fn extract_xml_paragraphs(xml: &str) -> Result<Vec<String>, WikiError> {
    let mut reader = XmlReader::from_str(xml);
    reader.config_mut().trim_text(true);
    let mut paragraphs = Vec::new();
    let mut current = String::new();
    let mut in_text = false;

    loop {
        match reader.read_event() {
            Ok(Event::Start(event)) => match local_name(event.name().as_ref()) {
                b"p" => current.clear(),
                b"t" => in_text = true,
                _ => {}
            },
            Ok(Event::End(event)) => match local_name(event.name().as_ref()) {
                b"p" => {
                    let paragraph = single_line(&decode_xml_entities(&current));
                    if !paragraph.is_empty() {
                        paragraphs.push(paragraph);
                    }
                    current.clear();
                }
                b"t" => in_text = false,
                _ => {}
            },
            Ok(Event::Text(text)) if in_text => {
                current.push_str(&String::from_utf8_lossy(text.as_ref()));
            }
            Ok(Event::CData(text)) if in_text => {
                current.push_str(&String::from_utf8_lossy(text.as_ref()));
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(error) => return Err(document_error(format!("parse xml: {error}"))),
        }
    }
    Ok(paragraphs)
}

fn markdown_table(rows: &[Vec<String>]) -> String {
    if rows.is_empty() {
        return String::new();
    }
    let column_count = rows.iter().map(Vec::len).max().unwrap_or(1).max(1);
    let mut markdown = String::new();
    let header = &rows[0];
    push_table_row(&mut markdown, header, column_count);
    let separators = vec!["---".to_string(); column_count];
    push_table_row(&mut markdown, &separators, column_count);
    for row in rows.iter().skip(1) {
        push_table_row(&mut markdown, row, column_count);
    }
    markdown.trim_end().to_string()
}

fn push_table_row(markdown: &mut String, row: &[String], column_count: usize) {
    markdown.push('|');
    for index in 0..column_count {
        markdown.push(' ');
        if let Some(cell) = row.get(index) {
            markdown.push_str(&escape_table_cell(cell));
        }
        markdown.push_str(" |");
    }
    markdown.push('\n');
}

fn cell_text(cell: &Data) -> String {
    match cell {
        Data::Empty => String::new(),
        _ => single_line(&cell.to_string()),
    }
}

fn escape_table_cell(cell: &str) -> String {
    single_line(cell).replace('|', "\\|")
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
    for child in element.children() {
        match child.value() {
            Node::Text(text) => parts.push(text.text.to_string()),
            Node::Element(_) => {
                if let Some(child_element) = ElementRef::wrap(child) {
                    collect_visible_text(child_element, parts);
                }
            }
            _ => {}
        }
    }
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

fn local_name(name: &[u8]) -> &[u8] {
    name.iter()
        .position(|byte| *byte == b':')
        .map_or(name, |index| &name[index + 1..])
}

fn slide_number(name: &str) -> Option<usize> {
    name.strip_prefix("ppt/slides/slide")?
        .strip_suffix(".xml")?
        .parse()
        .ok()
}

fn extension(file_name: &str) -> Option<String> {
    Path::new(file_name)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
}

fn decode_xml_entities(text: &str) -> String {
    html_escape::decode_html_entities(text).into_owned()
}

fn document_error(message: impl Into<String>) -> WikiError {
    WikiError::InvalidInput {
        field: "document",
        message: message.into(),
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;
    use std::path::Path;

    use super::*;
    use crate::ScopeIdentity;
    use crate::sources::SourceKind;
    use crate::store::MemoryWikiStore;

    fn zip_bytes(entries: &[(&str, &str)]) -> Vec<u8> {
        let cursor = std::io::Cursor::new(Vec::new());
        let mut zip = zip::ZipWriter::new(cursor);
        let options = zip::write::SimpleFileOptions::default();
        for (path, contents) in entries {
            zip.start_file(path, options).expect("start zip entry");
            zip.write_all(contents.as_bytes()).expect("write zip entry");
        }
        zip.finish().expect("finish zip").into_inner()
    }

    fn sample_docx() -> Vec<u8> {
        zip_bytes(&[(
            "word/document.xml",
            r#"<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Quarterly Brief</w:t></w:r></w:p><w:p><w:r><w:t>Revenue rose in Duluth.</w:t></w:r></w:p></w:body></w:document>"#,
        )])
    }

    fn sample_pptx() -> Vec<u8> {
        zip_bytes(&[
            (
                "ppt/slides/slide2.xml",
                r#"<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Second slide summary</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"#,
            ),
            (
                "ppt/slides/slide1.xml",
                r#"<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>First slide title</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"#,
            ),
        ])
    }

    fn sample_xlsx() -> Vec<u8> {
        zip_bytes(&[
            (
                "[Content_Types].xml",
                r#"<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>"#,
            ),
            (
                "_rels/.rels",
                r#"<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>"#,
            ),
            (
                "xl/workbook.xml",
                r#"<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>"#,
            ),
            (
                "xl/_rels/workbook.xml.rels",
                r#"<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>"#,
            ),
            (
                "xl/worksheets/sheet1.xml",
                r#"<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>City</t></is></c><c r="B1" t="inlineStr"><is><t>Count</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Duluth</t></is></c><c r="B2"><v>3</v></c></row></sheetData></worksheet>"#,
            ),
        ])
    }

    fn ingest_sample(
        temp: &Path,
        store: &mut MemoryWikiStore,
        file_name: &str,
        kind: SourceKind,
        bytes: Vec<u8>,
    ) -> DocumentIngestResult {
        ingest_document(
            temp,
            store,
            ScopeIdentity::project("project-123"),
            DocumentSnapshot {
                location: format!("/tmp/{file_name}"),
                file_name: file_name.to_string(),
                fetched_at: "2026-05-31T20:00:00Z".to_string(),
                bytes,
                kind,
            },
        )
        .expect("ingest document")
    }

    #[test]
    fn extracts_office_html_and_degrades() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let cases = [
            (
                "data.xlsx",
                SourceKind::Office,
                sample_xlsx(),
                "| City | Count |",
                "Duluth",
            ),
            (
                "brief.docx",
                SourceKind::Office,
                sample_docx(),
                "Quarterly Brief",
                "Revenue rose in Duluth.",
            ),
            (
                "deck.pptx",
                SourceKind::Office,
                sample_pptx(),
                "## Slide 1",
                "Second slide summary",
            ),
            (
                "page.html",
                SourceKind::Html,
                b"<!doctype html><html><head><title>Readable &amp; Useful</title><script>drop()</script></head><body><main><h1>Readable &amp; Useful</h1><p>Keep this body text.</p></main></body></html>".to_vec(),
                "# Readable & Useful",
                "Keep this body text.",
            ),
        ];

        for (file_name, kind, bytes, first_expected, second_expected) in cases {
            let result = ingest_sample(
                temp.path(),
                &mut store,
                file_name,
                kind.clone(),
                bytes.clone(),
            );
            assert_eq!(result.record.kind, kind);
            assert_eq!(
                std::fs::read(temp.path().join(&result.asset_path)).expect("asset bytes"),
                bytes
            );
            let document = store
                .documents
                .get(&result.derived_path)
                .expect("derived document indexed");
            assert!(document.body.contains(first_expected), "{file_name}");
            assert!(document.body.contains(second_expected), "{file_name}");
        }

        let degraded = ingest_sample(
            temp.path(),
            &mut store,
            "broken.docx",
            SourceKind::Office,
            b"not a zip".to_vec(),
        );

        assert_eq!(
            std::fs::read(temp.path().join(&degraded.asset_path)).expect("degraded asset"),
            b"not a zip"
        );
        assert!(degraded.document_degradation.is_some());
        let document = store
            .documents
            .get(&degraded.derived_path)
            .expect("degraded document indexed");
        assert!(
            document
                .body
                .contains("media_degradation: office_parse_error")
        );
        assert!(document.body.contains("## Document Parse Unavailable"));
    }

    #[test]
    fn office_html_degradation_uses_uniform_metadata() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let office_bytes = b"not a zip".to_vec();
        let office = ingest_sample(
            temp.path(),
            &mut store,
            "broken.xlsx",
            SourceKind::Office,
            office_bytes.clone(),
        );
        assert_eq!(
            std::fs::read(temp.path().join(&office.asset_path)).expect("office asset"),
            office_bytes
        );
        let office_doc = store
            .documents
            .get(&office.derived_path)
            .expect("office derived document indexed");
        assert!(
            office_doc
                .body
                .contains("media_degradation: office_parse_error")
        );
        assert!(
            office_doc
                .body
                .contains(&format!("file_size_bytes: {}", office_bytes.len()))
        );
        assert!(office_doc.body.contains("sheet_count: 0"));

        let html_bytes = b"<html><head></head><body><script>drop()</script></body></html>".to_vec();
        let html = ingest_sample(
            temp.path(),
            &mut store,
            "empty.html",
            SourceKind::Html,
            html_bytes.clone(),
        );
        assert_eq!(
            std::fs::read(temp.path().join(&html.asset_path)).expect("html asset"),
            html_bytes
        );
        let html_doc = store
            .documents
            .get(&html.derived_path)
            .expect("html derived document indexed");
        assert!(
            html_doc
                .body
                .contains("media_degradation: html_parse_error")
        );
        assert!(
            html_doc
                .body
                .contains(&format!("file_size_bytes: {}", html_bytes.len()))
        );
        assert!(html_doc.body.contains("page_count: 1"));
    }

    #[test]
    fn markdown_table_handles_empty_rows() {
        assert_eq!(markdown_table(&[]), "");
    }
}
