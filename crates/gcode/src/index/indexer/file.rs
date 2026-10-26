use std::collections::BTreeMap;
use std::path::Path;

use anyhow::Context as _;
use postgres::Client;

use crate::db::GraphFileFacts;
use crate::index::semantic::{self, SemanticCallResolver};
use crate::index::{chunker, hasher, languages, parser, walker};
use crate::models::{CallTargetKind, ContentChunk, IndexedFile};
use crate::node::ParsedFacts;
use crate::vector::code_symbols::vector_text_for_symbol;

use super::IndexTimings;
use super::selector_blob;
use super::sink::{CodeFactSink, PostgresCodeFactSink};
use super::types::{FileIndexCounts, IndexTarget};
use super::util::{epoch_secs_str, relative_path};

/// Index a single file. Returns symbol count or None if skipped.
pub(super) fn index_file(
    conn: &mut Client,
    file_path: &Path,
    target: IndexTarget<'_>,
    exclude_patterns: &[impl AsRef<str>],
    import_context: &parser::ImportResolutionContext,
    semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Option<FileIndexCounts>> {
    let mut timings = IndexTimings::new("file.parse");
    let rel = match relative_path(file_path, target.root_path) {
        Ok(rel) => rel,
        Err(_) => return Ok(None),
    };

    let Some(parse_result) = parser::parse_file_with_semantic(
        file_path,
        target.project_id,
        target.root_path,
        exclude_patterns,
        import_context,
        semantic_resolver,
    )?
    else {
        return Ok(None);
    };

    let language = languages::detect_language(&file_path.to_string_lossy()).unwrap_or("unknown");
    // Child facts were derived from this captured source. Re-reading the mutable
    // path here can mix snapshots when an editor writes during parsing, leaving
    // their content FK pointed at a parent row for different bytes.
    let h = hasher::content_hash(&parse_result.source);
    let size = parse_result.source.len();

    // PostgreSQL hub writes (transactional).
    timings.phase("file.transaction");
    let mut tx = conn
        .transaction()
        .context("start indexed file transaction")?;

    timings.phase("file.sink");
    let mut sink = PostgresCodeFactSink::new(
        &mut tx,
        target.project_id,
        target.root_path,
        target.mode,
        target.machine_id,
    )?;
    timings.phase("file.facts");
    let chunks = chunker::chunk_file_content(
        &parse_result.source,
        &rel,
        target.project_id,
        &h,
        Some(language),
    );
    let parsed = ParsedFacts {
        symbols: parse_result.symbols,
        imports: parse_result.imports,
        calls: parse_result.calls,
        inheritance: parse_result.inheritance,
        chunks,
    };
    let counts = write_parsed_file_facts(
        &mut sink,
        target.project_id,
        &rel,
        language,
        &h,
        size,
        &parsed,
    )?;
    let clean_blob = target.head_blobs.clean_blob(&rel, &parse_result.source);
    selector_blob::record_selector_blob(
        &mut tx,
        target.machine_id,
        target.project_id,
        &rel,
        &h,
        clean_blob.as_deref(),
    )?;

    timings.phase("file.commit");
    tx.commit().context("commit indexed file transaction")?;

    Ok(Some(counts))
}

pub(super) fn create_semantic_resolver_if_needed(
    root_path: &Path,
    candidates: &[std::path::PathBuf],
    require_cpp_semantics: bool,
) -> anyhow::Result<Option<Box<dyn SemanticCallResolver>>> {
    if !has_cpp_semantic_candidate(candidates) {
        return Ok(None);
    }
    semantic::create_cpp_semantic_resolver(root_path, require_cpp_semantics)
}

fn has_cpp_semantic_candidate(candidates: &[std::path::PathBuf]) -> bool {
    candidates.iter().any(|path| {
        matches!(
            languages::detect_language(&path.to_string_lossy()),
            Some("c" | "cpp")
        )
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum ExplicitFileRoute {
    Ast,
    ContentOnly,
    Skip,
}

pub(super) fn explicit_file_route(
    root_path: &Path,
    path: &Path,
    exclude_patterns: &[impl AsRef<str>],
) -> ExplicitFileRoute {
    match walker::classify_file(root_path, path, exclude_patterns) {
        Some(walker::FileClassification::Ast) => ExplicitFileRoute::Ast,
        Some(walker::FileClassification::ContentOnly) => ExplicitFileRoute::ContentOnly,
        None => ExplicitFileRoute::Skip,
    }
}

/// Index content-only file (no AST, just chunks).
pub(super) fn index_content_only(
    conn: &mut Client,
    path: &Path,
    target: IndexTarget<'_>,
    exclude_patterns: &[impl AsRef<str>],
) -> anyhow::Result<Option<FileIndexCounts>> {
    if !walker::is_content_indexable(target.root_path, path, exclude_patterns) {
        return Ok(None);
    }

    let rel = match relative_path(path, target.root_path) {
        Ok(r) => r,
        Err(_) => return Ok(None),
    };

    let source = match std::fs::read(path) {
        Ok(s) => s,
        Err(error) => {
            log::debug!(
                "skipping content-only index for unreadable file {}: {error}",
                path.display()
            );
            return Ok(None);
        }
    };

    let lang = walker::content_language(path);
    let content_hash = hasher::content_hash(&source);

    let mut tx = conn
        .transaction()
        .context("start content-only file transaction")?;
    let mut sink = PostgresCodeFactSink::new(
        &mut tx,
        target.project_id,
        target.root_path,
        target.mode,
        target.machine_id,
    )?;
    let chunks = chunker::chunk_file_content(
        &source,
        &rel,
        target.project_id,
        &content_hash,
        Some(lang.as_str()),
    );
    let counts = write_content_only_file_facts(
        &mut sink,
        target.project_id,
        &rel,
        &lang,
        &content_hash,
        source.len(),
        &chunks,
    )?;
    let clean_blob = target.head_blobs.clean_blob(&rel, &source);
    selector_blob::record_selector_blob(
        &mut tx,
        target.machine_id,
        target.project_id,
        &rel,
        &content_hash,
        clean_blob.as_deref(),
    )?;

    tx.commit()
        .context("commit content-only file transaction")?;
    Ok(Some(counts))
}

pub(super) fn write_parsed_file_facts(
    sink: &mut impl CodeFactSink,
    project_id: &str,
    rel: &str,
    language: &str,
    content_hash: &str,
    byte_size: usize,
    parsed: &ParsedFacts,
) -> anyhow::Result<FileIndexCounts> {
    let mut timings = IndexTimings::new("facts.file");
    let before = sink.read_version_facts(project_id, rel, content_hash)?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel, content_hash),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        language: language.to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: parsed.symbols.len(),
        byte_size,
        indexed_at: epoch_secs_str(),
    })?;
    timings.phase("facts.symbols");
    let symbols_indexed = sink.upsert_symbols(&parsed.symbols)?;
    let current_symbol_ids = parsed
        .symbols
        .iter()
        .map(|symbol| symbol.id.clone())
        .collect::<Vec<_>>();
    timings.phase("facts.prune_symbols");
    sink.delete_stale_file_symbols(project_id, rel, content_hash, &current_symbol_ids)?;
    timings.phase("facts.prune_other");
    sink.delete_file_non_symbol_facts(project_id, rel, content_hash)?;
    timings.phase("facts.imports");
    let imports_indexed = sink.upsert_imports(project_id, rel, content_hash, &parsed.imports)?;
    timings.phase("facts.calls");
    let calls_indexed = sink.upsert_calls(project_id, rel, content_hash, &parsed.calls)?;
    timings.phase("facts.inheritance");
    let _inheritance_indexed =
        sink.upsert_inheritance(project_id, rel, content_hash, &parsed.inheritance)?;
    let unresolved_targets_indexed = parsed
        .calls
        .iter()
        .filter(|call| call.callee_target_kind == CallTargetKind::Unresolved)
        .count();
    timings.phase("facts.write_chunks");
    let chunks_indexed = if parsed.chunks.is_empty() {
        0
    } else {
        sink.upsert_content_chunks(&parsed.chunks)?
    };

    timings.phase("facts.projection_reset");
    reset_changed_projections(sink, project_id, rel, content_hash, &before, parsed)?;

    Ok(FileIndexCounts {
        file_path: rel.to_string(),
        indexed_files: 1,
        symbols_indexed,
        imports_indexed,
        calls_indexed,
        unresolved_targets_indexed,
        chunks_indexed,
    })
}

/// Reset only the sync flags whose projection input this re-parse changed.
/// A version with no stored facts compares against the parse alone. Otherwise
/// the version is re-read after the writes and compared with its prior facts.
fn reset_changed_projections(
    sink: &mut impl CodeFactSink,
    project_id: &str,
    rel: &str,
    content_hash: &str,
    before: &GraphFileFacts,
    parsed: &ParsedFacts,
) -> anyhow::Result<()> {
    let (vectors, graph) = if before.definitions.is_empty()
        && before.imports.is_empty()
        && before.calls.is_empty()
        && before.inheritance.is_empty()
    {
        let vectors = !parsed.symbols.is_empty();
        let graph = vectors
            || !parsed.imports.is_empty()
            || !parsed.calls.is_empty()
            || !parsed.inheritance.is_empty();
        (vectors, graph)
    } else {
        let after = sink.read_version_facts(project_id, rel, content_hash)?;
        (
            vector_inputs(before) != vector_inputs(&after),
            graph_inputs_changed(before, &after),
        )
    };
    if vectors || graph {
        sink.dirty_version_sync(project_id, rel, content_hash, vectors, graph)?;
    }
    Ok(())
}

/// Vector text per symbol id. Upserts keep stored summaries, so the vector
/// input comes from the stored definitions rather than the parse.
fn vector_inputs(facts: &GraphFileFacts) -> BTreeMap<&str, String> {
    facts
        .definitions
        .iter()
        .map(|symbol| (symbol.id.as_str(), vector_text_for_symbol(symbol)))
        .collect()
}

/// Whether any graph projection input differs: the import, call and
/// inheritance rows, or the per-symbol fields the graph projection reads.
fn graph_inputs_changed(before: &GraphFileFacts, after: &GraphFileFacts) -> bool {
    before.imports != after.imports
        || before.calls != after.calls
        || before.inheritance != after.inheritance
        || symbol_graph_rows(before) != symbol_graph_rows(after)
}

fn symbol_graph_rows(facts: &GraphFileFacts) -> Vec<(&str, &str, &str, &str, &str, usize, usize)> {
    facts
        .definitions
        .iter()
        .map(|symbol| {
            (
                symbol.id.as_str(),
                symbol.name.as_str(),
                symbol.qualified_name.as_str(),
                symbol.kind.as_str(),
                symbol.language.as_str(),
                symbol.line_start,
                symbol.line_end,
            )
        })
        .collect()
}

pub(super) fn write_content_only_file_facts(
    sink: &mut impl CodeFactSink,
    project_id: &str,
    rel: &str,
    language: &str,
    content_hash: &str,
    byte_size: usize,
    chunks: &[ContentChunk],
) -> anyhow::Result<FileIndexCounts> {
    let before = sink.read_version_facts(project_id, rel, content_hash)?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel, content_hash),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        language: language.to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: 0,
        byte_size,
        indexed_at: epoch_secs_str(),
    })?;
    sink.delete_file_non_symbol_facts(project_id, rel, content_hash)?;
    // Symbols are left untouched, so only dropped graph rows change the graph input.
    if !before.imports.is_empty() || !before.calls.is_empty() || !before.inheritance.is_empty() {
        sink.dirty_version_sync(project_id, rel, content_hash, false, true)?;
    }
    let chunks_indexed = if chunks.is_empty() {
        0
    } else {
        sink.upsert_content_chunks(chunks)?
    };

    Ok(FileIndexCounts {
        file_path: rel.to_string(),
        indexed_files: 1,
        chunks_indexed,
        ..FileIndexCounts::default()
    })
}

#[cfg(test)]
mod tests {
    use std::fs;

    use tempfile::TempDir;

    use super::*;

    #[test]
    fn c_header_candidates_enable_cpp_semantic_resolution() {
        let tempdir = TempDir::new().expect("create tempdir");
        let header = tempdir.path().join("widget.h");
        fs::write(&header, "void widget_render(void);\n").expect("write header");

        assert!(has_cpp_semantic_candidate(&[header]));
    }

    #[test]
    fn cpp_header_candidates_enable_cpp_semantic_resolution() {
        let tempdir = TempDir::new().expect("create tempdir");
        let header = tempdir.path().join("widget.h");
        fs::write(&header, "namespace app { class Widget {}; }\n").expect("write header");

        assert!(has_cpp_semantic_candidate(&[header]));
    }

    #[test]
    fn objc_header_candidates_do_not_enable_cpp_semantic_resolution() {
        let tempdir = TempDir::new().expect("create tempdir");
        let header = tempdir.path().join("widget.h");
        fs::write(&header, "@interface Widget\n@end\n").expect("write header");

        assert!(!has_cpp_semantic_candidate(&[header]));
    }
}
