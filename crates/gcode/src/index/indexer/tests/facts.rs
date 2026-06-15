use super::super::file::write_parsed_file_facts;
use super::super::sink::CodeFactSink;
use crate::models::{
    CallRelation, CallTargetKind, ContentChunk, ImportRelation, IndexedFile, ParseResult, Symbol,
};

#[derive(Default)]
struct RecordingCodeFactSink {
    writes: Vec<&'static str>,
    files: usize,
    symbols: usize,
    stale_symbols: usize,
    imports: usize,
    calls: usize,
    unresolved_targets: usize,
    chunks: usize,
}

impl CodeFactSink for RecordingCodeFactSink {
    fn delete_file_facts(&mut self, _project_id: &str, _file_path: &str) -> anyhow::Result<()> {
        self.writes.push("delete");
        Ok(())
    }

    fn delete_file_non_symbol_facts(
        &mut self,
        _project_id: &str,
        _file_path: &str,
    ) -> anyhow::Result<()> {
        self.writes.push("delete_non_symbols");
        Ok(())
    }

    fn delete_stale_file_symbols(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        current_symbol_ids: &[String],
    ) -> anyhow::Result<usize> {
        self.writes.push("delete_stale_symbols");
        self.stale_symbols += current_symbol_ids.len();
        Ok(0)
    }

    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize> {
        self.writes.push("symbols");
        self.symbols += symbols.len();
        Ok(symbols.len())
    }

    fn upsert_file(&mut self, _file: &IndexedFile) -> anyhow::Result<()> {
        self.writes.push("file");
        self.files += 1;
        Ok(())
    }

    fn upsert_imports(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize> {
        self.writes.push("imports");
        self.imports += imports.len();
        Ok(imports.len())
    }

    fn upsert_calls(
        &mut self,
        _project_id: &str,
        _file_path: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize> {
        self.writes.push("calls");
        self.calls += calls.len();
        self.unresolved_targets += calls
            .iter()
            .filter(|call| call.callee_target_kind == CallTargetKind::Unresolved)
            .count();
        Ok(calls.len())
    }

    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize> {
        self.writes.push("chunks");
        self.chunks += chunks.len();
        Ok(chunks.len())
    }
}

#[test]
fn library_writes_all_code_facts() {
    let project_id = "project-1";
    let rel = "src/lib.rs";
    let source = b"use std::fmt;\nfn caller() {\n    missing();\n}\n";
    let caller_id = Symbol::make_id(project_id, rel, "caller", "function", 14);
    let parse_result = ParseResult {
        symbols: vec![Symbol {
            id: caller_id.clone(),
            project_id: project_id.to_string(),
            file_path: rel.to_string(),
            name: "caller".to_string(),
            qualified_name: "caller".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 14,
            byte_end: 45,
            line_start: 2,
            line_end: 4,
            signature: Some("fn caller()".to_string()),
            docstring: None,
            parent_symbol_id: None,
            content_hash: "hash-1".to_string(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        }],
        imports: vec![ImportRelation {
            file_path: rel.to_string(),
            module_name: "std::fmt".to_string(),
        }],
        calls: vec![CallRelation::new(
            caller_id,
            "missing".to_string(),
            rel.to_string(),
            3,
        )],
        source: source.to_vec(),
    };

    let mut sink = RecordingCodeFactSink::default();
    let counts = write_parsed_file_facts(
        &mut sink,
        project_id,
        rel,
        "rust",
        "hash-1",
        source.len(),
        &parse_result,
    )
    .expect("write parsed file facts");

    assert_eq!(
        sink.writes,
        vec![
            "symbols",
            "delete_stale_symbols",
            "delete_non_symbols",
            "file",
            "imports",
            "calls",
            "chunks"
        ]
    );
    assert_eq!(sink.files, 1);
    assert_eq!(sink.symbols, 1);
    assert_eq!(sink.stale_symbols, 1);
    assert_eq!(sink.imports, 1);
    assert_eq!(sink.calls, 1);
    assert_eq!(sink.unresolved_targets, 1);
    assert_eq!(sink.chunks, 1);
    assert_eq!(counts.indexed_files, 1);
    assert_eq!(counts.symbols_indexed, 1);
    assert_eq!(counts.imports_indexed, 1);
    assert_eq!(counts.calls_indexed, 1);
    assert_eq!(counts.unresolved_targets_indexed, 1);
    assert_eq!(counts.chunks_indexed, 1);
}
