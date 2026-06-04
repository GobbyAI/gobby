use std::io::{Cursor, Read};

use calamine::{Data, Reader, open_workbook_auto_from_rs};
use quick_xml::Reader as XmlReader;
use quick_xml::events::Event;
use zip::ZipArchive;

use crate::ingest::single_line;

use super::*;

/// Maximum spreadsheet sheets rendered into markdown during bounded extraction.
const MAX_SHEETS: usize = 8;
/// Maximum rows rendered per spreadsheet sheet before truncation is reported.
const MAX_ROWS_PER_SHEET: usize = 64;
/// Maximum columns rendered per spreadsheet sheet before truncation is reported.
const MAX_COLUMNS_PER_SHEET: usize = 16;
/// Maximum PPTX slides parsed during bounded extraction.
pub(crate) const MAX_SLIDES: usize = 200;
/// Maximum uncompressed XML bytes read from a ZIP entry.
pub(crate) const MAX_ENTRY_BYTES: u64 = 2 * 1024 * 1024;

pub(crate) fn extract_office_document(
    file_name: &str,
    bytes: &[u8],
) -> Result<DocumentExtraction, WikiError> {
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

pub(crate) fn extract_docx(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
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
        degradation: None,
    })
}

pub(crate) fn extract_pptx(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
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
    let slides_truncated = slide_names.len() > MAX_SLIDES;
    if slides_truncated {
        slide_names.truncate(MAX_SLIDES);
    }

    let mut markdown = String::new();
    let mut title = None;
    let mut slide_count = 0;
    for (index, name) in slide_names.iter().enumerate() {
        let xml = read_zip_entry_from_archive(&mut archive, name)?;
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
    if slides_truncated {
        markdown.push_str("_Slides truncated for bounded extraction._\n\n");
    }
    Ok(DocumentExtraction {
        title,
        markdown: markdown.trim_end().to_string(),
        units_label: "slide_count",
        units_count: slide_count,
        degradation: None,
    })
}

fn extract_spreadsheet(bytes: &[u8]) -> Result<DocumentExtraction, WikiError> {
    let cursor = Cursor::new(bytes);
    let mut workbook = open_workbook_auto_from_rs(cursor)
        .map_err(|error| document_error(format!("open spreadsheet: {error}")))?;
    let sheet_names = workbook.sheet_names().to_vec();
    if sheet_names.is_empty() {
        return Err(document_error("spreadsheet contained no sheets"));
    }

    let mut markdown = String::new();
    let mut rendered_sheets = 0;
    let mut title = None;
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
        if title.is_none() {
            title = Some(markdown_title(sheet_name));
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
        title,
        markdown: markdown.trim_end().to_string(),
        units_label: "sheet_count",
        units_count: rendered_sheets,
        degradation: None,
    })
}

fn read_zip_entry(bytes: &[u8], name: &str) -> Result<String, WikiError> {
    let mut archive = zip_archive(bytes)?;
    read_zip_entry_from_archive(&mut archive, name)
}

fn read_zip_entry_from_archive(
    archive: &mut ZipArchive<Cursor<&[u8]>>,
    name: &str,
) -> Result<String, WikiError> {
    let mut xml = String::new();
    let mut entry = archive
        .by_name(name)
        .map_err(|error| document_error(format!("read {name}: {error}")))?;
    if entry.size() > MAX_ENTRY_BYTES {
        return Err(document_error(format!(
            "{name} is {} bytes; maximum supported XML entry is {MAX_ENTRY_BYTES} bytes",
            entry.size()
        )));
    }
    entry
        .by_ref()
        .take(MAX_ENTRY_BYTES + 1)
        .read_to_string(&mut xml)
        .map_err(|error| document_error(format!("read {name}: {error}")))?;
    if xml.len() as u64 > MAX_ENTRY_BYTES {
        return Err(document_error(format!(
            "{name} exceeds maximum supported XML entry size of {MAX_ENTRY_BYTES} bytes"
        )));
    }
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

pub(crate) fn markdown_table(rows: &[Vec<String>]) -> String {
    if rows.is_empty() {
        return String::new();
    }
    let column_count = rows.iter().map(Vec::len).max().unwrap_or(1);
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
