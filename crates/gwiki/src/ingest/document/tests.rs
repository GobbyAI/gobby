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

fn oversized_pptx(slide_count: usize) -> Vec<u8> {
    let cursor = std::io::Cursor::new(Vec::new());
    let mut zip = zip::ZipWriter::new(cursor);
    let options = zip::write::SimpleFileOptions::default();
    for index in 1..=slide_count {
        zip.start_file(format!("ppt/slides/slide{index}.xml"), options)
            .expect("start slide");
        zip.write_all(
            br#"<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Slide</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"#,
        )
        .expect("write slide");
    }
    zip.finish().expect("finish zip").into_inner()
}

fn sample_xlsx() -> Vec<u8> {
    xlsx_with_sheet_data(
        r#"<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>City</t></is></c><c r="B1" t="inlineStr"><is><t>Count</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Duluth</t></is></c><c r="B2"><v>3</v></c></row></sheetData>"#,
    )
}

fn oversized_xlsx(row_count: usize) -> Vec<u8> {
    let rows = (1..=row_count)
        .map(|row| {
            format!(
                r#"<row r="{row}"><c r="A{row}" t="inlineStr"><is><t>City {row}</t></is></c></row>"#
            )
        })
        .collect::<String>();
    xlsx_with_sheet_data(&format!("<sheetData>{rows}</sheetData>"))
}

fn xlsx_with_sheet_data(sheet_data: &str) -> Vec<u8> {
    let worksheet = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{sheet_data}</worksheet>"#
    );

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
        ("xl/worksheets/sheet1.xml", worksheet.as_str()),
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
            .contains(&format!("file_size_bytes: \"{}\"", office_bytes.len()))
    );
    assert!(office_doc.body.contains("sheet_count: \"0\""));

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
    assert!(html_doc.body.contains("media_degradation: html_no_content"));
    assert!(
        html_doc
            .body
            .contains(&format!("file_size_bytes: \"{}\"", html_bytes.len()))
    );
    assert!(html_doc.body.contains("page_count: \"1\""));
}

#[test]
fn markdown_table_handles_empty_rows() {
    assert_eq!(markdown_table(&[]), "");
}

#[test]
fn office_zip_reads_are_bounded() {
    let oversized_xml = "x".repeat(MAX_ENTRY_BYTES as usize + 1);
    let error = extract_docx(&zip_bytes(&[("word/document.xml", &oversized_xml)]))
        .expect_err("oversized docx XML rejected");

    assert!(matches!(error, WikiError::InvalidInput { .. }));
    assert!(error.to_string().contains("maximum supported XML entry"));
}

#[test]
fn pptx_slide_count_is_bounded() {
    let extraction =
        extract_pptx(&oversized_pptx(MAX_SLIDES + 1)).expect("oversized slide deck is truncated");

    assert_eq!(extraction.units_count, MAX_SLIDES);
    let degradation = extraction
        .degradation
        .as_ref()
        .expect("truncated pptx reports degradation");
    assert_eq!(degradation.reason(), "office_bounded_extraction");
    assert_eq!(degradation.unit_count.key(), "slide_count");
    assert_eq!(degradation.unit_count.count(), MAX_SLIDES);
    assert!(degradation.fallback.contains("MAX_SLIDES"));
    assert!(
        extraction
            .markdown
            .contains("_Slides truncated for bounded extraction._")
    );
}

#[test]
fn xlsx_table_bounds_report_degradation() {
    let extraction =
        extract_office_document("oversized.xlsx", &oversized_xlsx(MAX_ROWS_PER_SHEET + 1))
            .expect("oversized spreadsheet is truncated");

    assert_eq!(extraction.units_label, "sheet_count");
    assert_eq!(extraction.units_count, 1);
    assert!(
        extraction
            .markdown
            .contains("_Table truncated for bounded extraction._")
    );
    let degradation = extraction
        .degradation
        .as_ref()
        .expect("truncated xlsx reports degradation");
    assert_eq!(degradation.reason(), "office_bounded_extraction");
    assert_eq!(degradation.unit_count.key(), "sheet_count");
    assert_eq!(degradation.unit_count.count(), 1);
    assert!(degradation.fallback.contains("MAX_ROWS_PER_SHEET"));
}

#[test]
fn html_extraction_combines_inline_text_nodes() {
    let extraction = extract_html_document(
        b"<html><body><p>Hello <strong>world</strong>.</p><p>Next line.</p></body></html>",
    )
    .expect("html extracts");

    assert_eq!(extraction.markdown, "Hello world.\n\nNext line.");
}

#[test]
fn html_extraction_avoids_spaces_before_closing_quotes() {
    let extraction = extract_html_document(
        b"<html><body><p>Hello <span>world</span><span>\xe2\x80\x9d</span>.</p></body></html>",
    )
    .expect("html extracts");

    assert_eq!(extraction.markdown, "Hello world\u{201d}.");
}
