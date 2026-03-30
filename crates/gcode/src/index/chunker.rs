//! Content chunking: 100-line chunks with 10-line overlap.
//! Ports logic from src/gobby/code_index/chunker.py.

use crate::models::ContentChunk;

const CHUNK_SIZE: usize = 100;
const CHUNK_OVERLAP: usize = 10;

/// Split file content into overlapping chunks for FTS indexing.
pub fn chunk_file_content(
    source: &[u8],
    rel_path: &str,
    project_id: &str,
    language: Option<&str>,
) -> Vec<ContentChunk> {
    let text = String::from_utf8_lossy(source);
    let lines: Vec<&str> = text.split('\n').collect();
    if lines.is_empty() {
        return Vec::new();
    }

    let step = CHUNK_SIZE.saturating_sub(CHUNK_OVERLAP).max(1);
    let mut chunks = Vec::new();
    let mut chunk_index: usize = 0;
    let mut start = 0;

    while start < lines.len() {
        let end = (start + CHUNK_SIZE).min(lines.len());
        let chunk_content: String = lines[start..end].join("\n");

        if !chunk_content.trim().is_empty() {
            chunks.push(ContentChunk {
                id: ContentChunk::make_id(project_id, rel_path, chunk_index),
                project_id: project_id.to_string(),
                file_path: rel_path.to_string(),
                chunk_index,
                line_start: start + 1,
                line_end: end,
                content: chunk_content,
                language: language.unwrap_or("unknown").to_string(),
                created_at: iso_now(),
            });
            chunk_index += 1;
        }

        if end >= lines.len() {
            break;
        }
        start += step;
    }

    chunks
}

fn iso_now() -> String {
    // Minimal ISO-8601 without chrono dependency
    use std::time::SystemTime;
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("{secs}")
}
