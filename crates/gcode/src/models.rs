use postgres::Row;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::utils::i64_to_usize;

/// Stable namespace for deterministic symbol UUIDs.
/// Must match Python: uuid.UUID("c0de1de0-0000-4000-8000-000000000000")
pub const CODE_INDEX_UUID_NAMESPACE: Uuid = Uuid::from_bytes([
    0xc0, 0xde, 0x1d, 0xe0, 0x00, 0x00, 0x40, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
]);

pub const SOURCE_SYSTEM_GCODE: &str = "gcode";

/// Producer confidence classification for graph and vector projection facts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProjectionProvenance {
    Extracted,
    Inferred,
    Ambiguous,
}

impl ProjectionProvenance {
    pub fn from_wire_value(value: &str) -> Option<Self> {
        match value {
            "EXTRACTED" | "extracted" => Some(Self::Extracted),
            "INFERRED" | "inferred" => Some(Self::Inferred),
            "AMBIGUOUS" | "ambiguous" => Some(Self::Ambiguous),
            _ => None,
        }
    }
}

/// Optional provenance attached to graph results and projection payloads.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ProjectionMetadata {
    pub provenance: ProjectionProvenance,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    pub source_system: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_file_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_line: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_symbol_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub matching_method: Option<String>,
}

impl ProjectionMetadata {
    pub fn new(provenance: ProjectionProvenance, source_system: impl Into<String>) -> Self {
        Self {
            provenance,
            confidence: None,
            source_system: source_system.into(),
            source_file_path: None,
            source_line: None,
            source_symbol_id: None,
            matching_method: None,
        }
    }

    pub fn gcode_extracted() -> Self {
        Self::new(ProjectionProvenance::Extracted, SOURCE_SYSTEM_GCODE).with_confidence(Some(1.0))
    }

    pub fn inferred(source_system: impl Into<String>, confidence: Option<f64>) -> Self {
        Self::new(ProjectionProvenance::Inferred, source_system).with_confidence(confidence)
    }

    pub fn ambiguous(source_system: impl Into<String>, confidence: Option<f64>) -> Self {
        Self::new(ProjectionProvenance::Ambiguous, source_system).with_confidence(confidence)
    }

    pub fn with_confidence(mut self, confidence: Option<f64>) -> Self {
        self.confidence = confidence;
        self
    }

    pub fn with_source_file_path(mut self, file_path: impl Into<String>) -> Self {
        self.source_file_path = Some(file_path.into());
        self
    }

    pub fn with_source_line(mut self, line: usize) -> Self {
        self.source_line = Some(line);
        self
    }

    pub fn with_source_symbol_id(mut self, symbol_id: impl Into<String>) -> Self {
        self.source_symbol_id = Some(symbol_id.into());
        self
    }

    pub fn with_matching_method(mut self, matching_method: impl Into<String>) -> Self {
        self.matching_method = Some(matching_method.into());
        self
    }

    pub fn is_hypothesis(&self) -> bool {
        matches!(
            self.provenance,
            ProjectionProvenance::Inferred | ProjectionProvenance::Ambiguous
        )
    }
}

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

    /// Read a Symbol from a PostgreSQL row.
    ///
    /// Callers should select via `crate::db::symbol_select_columns()` so integer
    /// and timestamp fields are cast to stable Rust-readable types.
    pub fn from_row(row: &Row) -> anyhow::Result<Self> {
        Ok(Self {
            id: row.try_get("id")?,
            project_id: row.try_get("project_id")?,
            file_path: row.try_get("file_path")?,
            name: row.try_get("name")?,
            qualified_name: row.try_get("qualified_name")?,
            kind: row.try_get("kind")?,
            language: row.try_get("language")?,
            byte_start: i64_to_usize(row.try_get("byte_start")?, "byte_start")?,
            byte_end: i64_to_usize(row.try_get("byte_end")?, "byte_end")?,
            line_start: i64_to_usize(row.try_get("line_start")?, "line_start")?,
            line_end: i64_to_usize(row.try_get("line_end")?, "line_end")?,
            signature: row.try_get("signature")?,
            docstring: row.try_get("docstring")?,
            parent_symbol_id: row.try_get("parent_symbol_id")?,
            content_hash: row
                .try_get::<_, Option<String>>("content_hash")?
                .unwrap_or_default(),
            summary: row.try_get("summary")?,
            created_at: row
                .try_get::<_, Option<String>>("created_at")?
                .unwrap_or_default(),
            updated_at: row
                .try_get::<_, Option<String>>("updated_at")?
                .unwrap_or_default(),
        })
    }

    /// Slim representation for outline output.
    pub fn to_outline(&self) -> OutlineSymbol {
        OutlineSymbol {
            id: self.id.clone(),
            name: self.name.clone(),
            kind: self.kind.clone(),
            line_start: self.line_start,
            line_end: self.line_end,
            signature: self.signature.clone(),
        }
    }

    /// Brief dict-like representation for search results.
    pub fn to_brief(&self) -> SearchResult {
        SearchResult {
            id: self.id.clone(),
            name: self.name.clone(),
            qualified_name: self.qualified_name.clone(),
            kind: self.kind.clone(),
            language: self.language.clone(),
            file_path: self.file_path.clone(),
            line_start: self.line_start,
            line_end: self.line_end,
            score: 0.0,
            rrf_score: None,
            summary: self.summary.clone(),
            signature: self.signature.clone(),
            sources: None,
        }
    }
}

pub fn make_unresolved_callee_id(project_id: &str, callee_name: &str) -> String {
    let key = format!("unresolved:{project_id}:{callee_name}");
    Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, key.as_bytes()).to_string()
}

pub fn make_external_symbol_id(
    project_id: &str,
    callee_name: &str,
    module: Option<&str>,
) -> String {
    let module_key = module.unwrap_or_default();
    let key = format!("external:{project_id}:{module_key}:{callee_name}");
    Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, key.as_bytes()).to_string()
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
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallTargetKind {
    Symbol,
    Unresolved,
    External,
    /// Transient marker for a cross-file local import whose canonical target is
    /// resolved against `code_symbols` in a post-write pass (see
    /// `index::indexer::local_imports`). After resolution a row is rewritten to
    /// `Symbol` (hit) or `Unresolved` (miss), so this kind never persists past a
    /// completed index run.
    LocalImport,
}

impl CallTargetKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Symbol => "symbol",
            Self::Unresolved => "unresolved",
            Self::External => "external",
            Self::LocalImport => "local_import",
        }
    }
}

/// Call relationship extracted from AST.
#[derive(Debug, Clone)]
pub struct CallRelation {
    pub caller_symbol_id: String,
    pub callee_symbol_id: Option<String>,
    pub callee_name: String,
    pub callee_target_kind: CallTargetKind,
    pub callee_external_module: Option<String>,
    pub file_path: String,
    pub line: usize,
}

impl CallRelation {
    pub fn new(
        caller_symbol_id: String,
        callee_name: String,
        file_path: String,
        line: usize,
    ) -> Self {
        Self {
            caller_symbol_id,
            callee_symbol_id: None,
            callee_name,
            callee_target_kind: CallTargetKind::Unresolved,
            callee_external_module: None,
            file_path,
            line,
        }
    }

    pub fn with_symbol_target(mut self, callee_symbol_id: String) -> Self {
        self.callee_symbol_id = Some(callee_symbol_id);
        self.callee_target_kind = CallTargetKind::Symbol;
        self
    }

    pub fn with_external_target(
        mut self,
        callee_name: String,
        callee_external_module: String,
    ) -> Self {
        self.callee_name = callee_name;
        self.callee_target_kind = CallTargetKind::External;
        self.callee_external_module = Some(callee_external_module);
        self
    }

    /// Mark this call as a pending cross-file local import. `callee_name` is the
    /// originally imported name (not the local alias) and `candidate_files` are
    /// the project-relative files the target symbol might live in, derived from
    /// the import by pure path logic (no file reads). The post-write resolution
    /// pass (`index::indexer::local_imports`) looks the target up in
    /// `code_symbols` and rewrites this row to `Symbol` or `Unresolved`.
    ///
    /// Candidate files ride in `callee_external_module` joined by `\n`; the
    /// column is unused for local imports otherwise and is cleared on resolution.
    pub fn with_local_import_target(
        mut self,
        callee_name: String,
        candidate_files: Vec<String>,
    ) -> Self {
        self.callee_name = callee_name;
        self.callee_target_kind = CallTargetKind::LocalImport;
        self.callee_symbol_id = None;
        self.callee_external_module = Some(candidate_files.join(LOCAL_IMPORT_CANDIDATE_SEP));
        self
    }

    /// Candidate target files carried by a `LocalImport` call, parsed back out of
    /// `callee_external_module`. Empty for any other kind.
    pub fn local_import_candidate_files(&self) -> Vec<String> {
        if self.callee_target_kind != CallTargetKind::LocalImport {
            return Vec::new();
        }
        self.callee_external_module
            .as_deref()
            .map(|joined| {
                joined
                    .split(LOCAL_IMPORT_CANDIDATE_SEP)
                    .filter(|part| !part.is_empty())
                    .map(ToOwned::to_owned)
                    .collect()
            })
            .unwrap_or_default()
    }
}

/// Separator for the candidate-file list carried in `callee_external_module`
/// while a call is a pending `LocalImport`. Newlines never appear in project
/// file paths, so the join/split round-trips losslessly.
pub const LOCAL_IMPORT_CANDIDATE_SEP: &str = "\n";

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
    pub language: String,
    pub file_path: String,
    pub line_start: usize,
    pub line_end: usize,
    pub score: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rrf_score: Option<f64>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<ProjectionMetadata>,
}

/// Result of parsing a single file.
pub struct ParseResult {
    pub symbols: Vec<Symbol>,
    pub imports: Vec<ImportRelation>,
    pub calls: Vec<CallRelation>,
    /// Raw file bytes — carried through for body snippet extraction at embedding time.
    pub source: Vec<u8>,
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

/// Paginated response envelope for JSON output.
/// Hoists `project_id` to avoid repeating it on every result.
#[derive(Debug, Clone, Serialize)]
pub struct PagedResponse<T: Serialize> {
    pub project_id: String,
    pub total: usize,
    pub offset: usize,
    pub limit: usize,
    pub results: Vec<T>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hint: Option<String>,
}

/// Slim symbol for outline output — only what agents need.
#[derive(Debug, Clone, Serialize)]
pub struct OutlineSymbol {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub line_start: usize,
    pub line_end: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
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
    fn symbol_make_id_matches_python_uuid5_golden_vectors() {
        assert_eq!(
            CODE_INDEX_UUID_NAMESPACE.to_string(),
            "c0de1de0-0000-4000-8000-000000000000"
        );

        let cases = [
            (
                "proj1",
                "src/main.py",
                "foo",
                "function",
                42,
                "403e2117-92e7-5390-ad83-226629486481",
            ),
            (
                "3bf57fe7-2a0c-4074-8912-a83d9cd4df01",
                "crates/gcode/src/models.rs",
                "Symbol",
                "struct",
                111,
                "d28e80d3-a95e-5c2a-91c3-92551f75a2b1",
            ),
            (
                "proj-with-dashes",
                "src/lib.rs",
                "Widget::render",
                "method",
                0,
                "44da4f31-7218-5b3b-97c4-5a5eca9f0451",
            ),
            (
                "overlay:child",
                "nested/path/file.ts",
                "HTTPClient.new",
                "class",
                987654321,
                "f9531553-f2a7-5425-b487-6fb5b31d57bb",
            ),
        ];

        for (project_id, file_path, name, kind, byte_start, expected) in cases {
            assert_eq!(
                Symbol::make_id(project_id, file_path, name, kind, byte_start),
                expected,
                "Python UUID5 parity failed for {project_id}:{file_path}:{name}:{kind}:{byte_start}"
            );
        }
    }

    #[test]
    fn unresolved_and_external_ids_match_python_uuid5_golden_vectors() {
        assert_eq!(
            make_unresolved_callee_id("proj1", "missing_func"),
            "42693df1-99e6-5daa-be29-3535096cd2b5"
        );
        assert_eq!(
            make_external_symbol_id("proj1", "get", Some("requests")),
            "7c7e6ebe-47c6-5a3d-a83d-d5160f10cb74"
        );
        assert_eq!(
            make_external_symbol_id("proj1", "println", None),
            "c6b97498-448e-5ef1-9cb5-ab1cf37b6596"
        );
    }
    #[test]
    fn test_call_relation_promotes_symbol_targets() {
        let call = CallRelation::new(
            "caller-id".to_string(),
            "foo".to_string(),
            "src/main.py".to_string(),
            12,
        )
        .with_symbol_target("callee-id".to_string());

        assert_eq!(call.callee_symbol_id.as_deref(), Some("callee-id"));
        assert_eq!(call.callee_target_kind, CallTargetKind::Symbol);
    }

    #[test]
    fn graph_result_metadata_remains_optional_in_json_contract() {
        let json = serde_json::json!({
            "id": "sym-1",
            "name": "foo",
            "file_path": "src/main.rs",
            "line": 10
        });

        let parsed: GraphResult =
            serde_json::from_value(json).expect("graph result JSON parses without metadata");
        assert!(parsed.metadata.is_none());

        let serialized = serde_json::to_value(&parsed).expect("graph result serializes");
        assert!(serialized.get("metadata").is_none());
    }

    #[test]
    fn graph_result_without_metadata_omits_metadata_when_serialized() {
        let strategy = (
            proptest::string::string_regex("[ -~]{0,32}").expect("valid id regex"),
            proptest::string::string_regex("[ -~]{0,32}").expect("valid name regex"),
            proptest::string::string_regex("[ -~]{0,64}").expect("valid path regex"),
            0usize..1_000_000,
            proptest::option::of(
                proptest::string::string_regex("[ -~]{0,32}").expect("valid relation regex"),
            ),
            proptest::option::of(0usize..1_000),
        );

        proptest::test_runner::TestRunner::default()
            .run(
                &strategy,
                |(id, name, file_path, line, relation, distance)| {
                    let result = GraphResult {
                        id,
                        name,
                        file_path,
                        line,
                        relation,
                        distance,
                        metadata: None,
                    };

                    let serialized =
                        serde_json::to_value(&result).expect("graph result serializes");
                    assert_eq!(serialized.get("metadata"), None);

                    Ok(())
                },
            )
            .expect("metadata omission property holds");
    }
}
