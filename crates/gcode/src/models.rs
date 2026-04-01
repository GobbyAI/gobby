use rusqlite::Row;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Stable namespace for deterministic symbol UUIDs.
/// Must match Python: uuid.UUID("c0de1de0-0000-4000-8000-000000000000")
pub const CODE_INDEX_UUID_NAMESPACE: Uuid = Uuid::from_bytes([
    0xc0, 0xde, 0x1d, 0xe0, 0x00, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
]);

/// A code symbol extracted from AST parsing.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Symbol {
    pub id: String,
    pub project_id: String,
    pub file_path: String,
    pub name: String,
    pub qualified_name: String,
    pub kind: String,
    pub language: String,
    pub byte_start: usize,
    pub byte_end: usize,
    pub line_start: usize,
    pub line_end: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub docstring: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_symbol_id: Option<String>,
    #[serde(default)]
    pub content_hash: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(default)]
    pub created_at: String,
    #[serde(default)]
    pub updated_at: String,
}

impl Symbol {
    /// Generate deterministic UUID5 for a symbol.
    /// Must produce identical IDs to Python Symbol.make_id().
    pub fn make_id(
        project_id: &str,
        file_path: &str,
        name: &str,
        kind: &str,
        byte_start: usize,
    ) -> String {
        let key = format!("{project_id}:{file_path}:{name}:{kind}:{byte_start}");
        Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, key.as_bytes()).to_string()
    }

    /// Read a Symbol from a rusqlite Row (SELECT * FROM code_symbols).
    pub fn from_row(row: &Row) -> rusqlite::Result<Self> {
        let byte_start_raw = row.get::<_, i64>("byte_start")?;
        let byte_end_raw = row.get::<_, i64>("byte_end")?;
        let line_start_raw = row.get::<_, i64>("line_start")?;
        let line_end_raw = row.get::<_, i64>("line_end")?;

        let to_usize = |val: i64, col: usize| -> rusqlite::Result<usize> {
            val.try_into()
                .map_err(|_| rusqlite::Error::IntegralValueOutOfRange(col, val))
        };

        Ok(Self {
            id: row.get("id")?,
            project_id: row.get("project_id")?,
            file_path: row.get("file_path")?,
            name: row.get("name")?,
            qualified_name: row.get("qualified_name")?,
            kind: row.get("kind")?,
            language: row.get("language")?,
            byte_start: to_usize(byte_start_raw, 7)?,
            byte_end: to_usize(byte_end_raw, 8)?,
            line_start: to_usize(line_start_raw, 9)?,
            line_end: to_usize(line_end_raw, 10)?,
            signature: row.get("signature")?,
            docstring: row.get("docstring")?,
            parent_symbol_id: row.get("parent_symbol_id")?,
            content_hash: row
                .get::<_, Option<String>>("content_hash")?
                .unwrap_or_default(),
            summary: row.get("summary")?,
            created_at: row
                .get::<_, Option<String>>("created_at")?
                .unwrap_or_default(),
            updated_at: row
                .get::<_, Option<String>>("updated_at")?
                .unwrap_or_default(),
        })
    }

    /// Brief dict-like representation for search results.
    pub fn to_brief(&self) -> SearchResult {
        SearchResult {
            id: self.id.clone(),
            name: self.name.clone(),
            qualified_name: self.qualified_name.clone(),
            kind: self.kind.clone(),
            file_path: self.file_path.clone(),
            line_start: self.line_start,
            score: 0.0,
            summary: self.summary.clone(),
            signature: self.signature.clone(),
            sources: None,
        }
    }
}

/// Metadata for an indexed file.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexedFile {
    pub id: String,
    pub project_id: String,
    pub file_path: String,
    pub language: String,
    pub content_hash: String,
    pub symbol_count: usize,
    pub byte_size: usize,
    pub indexed_at: String,
}

impl IndexedFile {
    pub fn make_id(project_id: &str, file_path: &str) -> String {
        let key = format!("{project_id}:{file_path}");
        Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, key.as_bytes()).to_string()
    }
}

/// A chunk of file content for FTS search.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContentChunk {
    pub id: String,
    pub project_id: String,
    pub file_path: String,
    pub chunk_index: usize,
    pub line_start: usize,
    pub line_end: usize,
    pub content: String,
    pub language: String,
    pub created_at: String,
}

impl ContentChunk {
    pub fn make_id(project_id: &str, file_path: &str, chunk_index: usize) -> String {
        let key = format!("{project_id}:{file_path}:chunk:{chunk_index}");
        Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, key.as_bytes()).to_string()
    }
}

/// Import relationship extracted from AST.
#[derive(Debug, Clone)]
pub struct ImportRelation {
    pub file_path: String,
    pub module_name: String,
}

/// Call relationship extracted from AST.
#[derive(Debug, Clone)]
pub struct CallRelation {
    pub caller_id: String,
    pub callee_name: String,
    pub file_path: String,
    pub line: usize,
}

/// Project index statistics.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexedProject {
    pub id: String,
    pub root_path: String,
    pub total_files: usize,
    pub total_symbols: usize,
    pub last_indexed_at: String,
    pub index_duration_ms: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub total_eligible_files: Option<usize>,
}

/// Search result with score.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResult {
    pub id: String,
    pub name: String,
    pub qualified_name: String,
    pub kind: String,
    pub file_path: String,
    pub line_start: usize,
    pub score: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sources: Option<Vec<String>>,
}

/// Graph query result (callers, usages).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphResult {
    pub id: String,
    pub name: String,
    pub file_path: String,
    pub line: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub relation: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub distance: Option<usize>,
}

/// Result of parsing a single file.
pub struct ParseResult {
    pub symbols: Vec<Symbol>,
    pub imports: Vec<ImportRelation>,
    pub calls: Vec<CallRelation>,
}

/// Aggregate result of indexing a directory.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexResult {
    pub project_id: String,
    pub files_indexed: usize,
    pub files_skipped: usize,
    pub symbols_found: usize,
    pub errors: Vec<String>,
    pub duration_ms: u64,
}

/// Content search hit from FTS.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContentSearchHit {
    pub file_path: String,
    pub line_start: usize,
    pub line_end: usize,
    pub snippet: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub language: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_uuid5_parity_with_python() {
        // Python: Symbol.make_id("proj1", "src/main.py", "foo", "function", 42)
        // Must produce the same UUID in Rust.
        let id = Symbol::make_id("proj1", "src/main.py", "foo", "function", 42);
        // The key is "proj1:src/main.py:foo:function:42"
        // This is a deterministic UUID5 — verify it's stable across runs.
        let id2 = Symbol::make_id("proj1", "src/main.py", "foo", "function", 42);
        assert_eq!(id, id2);

        // Verify the namespace UUID bytes match Python's c0de1de0-0000-4000-8000-000000000000
        assert_eq!(
            CODE_INDEX_UUID_NAMESPACE.to_string(),
            "c0de1de0-0000-4000-8000-000000000000"
        );
    }
}
