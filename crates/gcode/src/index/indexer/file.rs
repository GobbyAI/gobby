use std::path::Path;

use anyhow::Context as _;
use postgres::Client;

use crate::index::semantic::{self, SemanticCallResolver};
use crate::index::{chunker, hasher, languages, parser, walker};
use crate::models::{CallTargetKind, IndexedFile, ParseResult};

use super::sink::{CodeFactSink, PostgresCodeFactSink};
use super::types::FileIndexCounts;
use super::util::{epoch_secs_str, relative_path};

/// Index a single file. Returns symbol count or None if skipped.
pub(super) fn index_file(
    conn: &mut Client,
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[impl AsRef<str>],
    import_context: &parser::ImportResolutionContext,
    semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> anyhow::Result<Option<FileIndexCounts>> {
    let rel = match relative_path(file_path, root_path) {
        Ok(rel) => rel,
        Err(_) => return Ok(None),
    };

    let Some(parse_result) = parser::parse_file_with_semantic(
        file_path,
        project_id,
        root_path,
        exclude_patterns,
        import_context,
        semantic_resolver,
    )?
    else {
        return Ok(None);
    };

    let language = languages::detect_language(&file_path.to_string_lossy()).unwrap_or("unknown");
    let h = match hasher::file_content_hash(file_path) {
        Ok(hash) => hash,
        Err(error) => {
            log::debug!(
                "skipping AST index for unreadable file {}: {error}",
                file_path.display()
            );
            return Ok(None);
        }
    };
    let size = match file_path.metadata() {
        Ok(metadata) => match usize::try_from(metadata.len()) {
            Ok(size) => size,
            Err(error) => {
                log::warn!(
                    "skipping AST index for file with unsupported size {}: {error}",
                    file_path.display()
                );
                return Ok(None);
            }
        },
        Err(error) => {
            log::warn!(
                "skipping AST index for file with unreadable metadata {}: {error}",
                file_path.display()
            );
            return Ok(None);
        }
    };

    // PostgreSQL hub writes (transactional).
    let mut tx = conn
        .transaction()
        .context("start indexed file transaction")?;

    let mut sink = PostgresCodeFactSink::new(&mut tx, project_id, root_path)?;
    let counts = write_parsed_file_facts(
        &mut sink,
        project_id,
        &rel,
        language,
        &h,
        size,
        &parse_result,
    )?;

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
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[impl AsRef<str>],
) -> anyhow::Result<Option<FileIndexCounts>> {
    if !walker::is_content_indexable(root_path, path, exclude_patterns) {
        return Ok(None);
    }

    let rel = match relative_path(path, root_path) {
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
    let mut sink = PostgresCodeFactSink::new(&mut tx, project_id, root_path)?;
    let counts = write_content_only_file_facts(
        &mut sink,
        project_id,
        &rel,
        &lang,
        &content_hash,
        source.len(),
        &source,
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
    parse_result: &ParseResult,
) -> anyhow::Result<FileIndexCounts> {
    let symbols_indexed = sink.upsert_symbols(&parse_result.symbols)?;
    let current_symbol_ids = parse_result
        .symbols
        .iter()
        .map(|symbol| symbol.id.clone())
        .collect::<Vec<_>>();
    sink.delete_stale_file_symbols(project_id, rel, &current_symbol_ids)?;
    sink.delete_file_non_symbol_facts(project_id, rel)?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        language: language.to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: parse_result.symbols.len(),
        byte_size,
        indexed_at: epoch_secs_str(),
    })?;
    let imports_indexed = sink.upsert_imports(project_id, rel, &parse_result.imports)?;
    let calls_indexed = sink.upsert_calls(project_id, rel, &parse_result.calls)?;
    let unresolved_targets_indexed = parse_result
        .calls
        .iter()
        .filter(|call| call.callee_target_kind == CallTargetKind::Unresolved)
        .count();
    let chunks = chunker::chunk_file_content(&parse_result.source, rel, project_id, Some(language));
    let chunks_indexed = if chunks.is_empty() {
        0
    } else {
        sink.upsert_content_chunks(&chunks)?
    };

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

pub(super) fn write_content_only_file_facts(
    sink: &mut impl CodeFactSink,
    project_id: &str,
    rel: &str,
    language: &str,
    content_hash: &str,
    byte_size: usize,
    source: &[u8],
) -> anyhow::Result<FileIndexCounts> {
    sink.delete_file_facts(project_id, rel)?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        language: language.to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: 0,
        byte_size,
        indexed_at: epoch_secs_str(),
    })?;
    let chunks = chunker::chunk_file_content(source, rel, project_id, Some(language));
    let chunks_indexed = if chunks.is_empty() {
        0
    } else {
        sink.upsert_content_chunks(&chunks)?
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
