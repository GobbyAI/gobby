use crate::codewiki_facts::SymbolFact;

use super::contracts::{
    CommitMetadataEvidence, Completeness, EvidenceItem, EvidenceWarning, ReadSelector,
    SourceEvidence,
};
use super::snapshot::{canonical_hash, validate_repo_path};
use super::{EvidenceError, EvidenceLibrary, QueryResult, Result};

pub(super) fn execute(library: &EvidenceLibrary, selector: &ReadSelector) -> Result<QueryResult> {
    let items = match selector {
        ReadSelector::Range {
            path,
            start_line,
            end_line,
        } => vec![EvidenceItem::Source(source_for_lines(
            library,
            path,
            *start_line,
            *end_line,
            None,
        )?)],
        ReadSelector::Symbol {
            path,
            qualified_name,
        } => {
            validate_repo_path(path)?;
            library.validate_index_inventory()?;
            let matches = library
                .facts
                .symbols_for_file(path)
                .map_err(|error| EvidenceError::IndexUnavailable {
                    reason: format!("{error:#}"),
                })?
                .into_iter()
                .filter(|symbol| symbol.qualified_name == *qualified_name)
                .collect::<Vec<_>>();
            if matches.len() != 1 {
                return Err(EvidenceError::InvalidSelector {
                    detail: format!(
                        "expected one exact symbol {qualified_name:?} in {path}, found {}",
                        matches.len()
                    ),
                });
            }
            vec![EvidenceItem::Source(source_for_symbol(
                library,
                &matches[0],
            )?)]
        }
        ReadSelector::CommitMetadata => commit_items(library)?,
    };
    let result_limit = items.len();
    Ok(QueryResult {
        completeness: if items.is_empty() {
            Completeness::CompleteEmpty
        } else {
            Completeness::Complete
        },
        items,
        lane: match selector {
            ReadSelector::CommitMetadata => "read_commit_metadata",
            ReadSelector::Range { .. } => "read_range",
            ReadSelector::Symbol { .. } => "read_symbol",
        }
        .to_string(),
        hybrid: None,
        exclusions: Vec::new(),
        warnings: Vec::<EvidenceWarning>::new(),
        result_limit,
        graph_depth: None,
    })
}

pub(super) fn source_for_symbol(
    library: &EvidenceLibrary,
    symbol: &SymbolFact,
) -> Result<SourceEvidence> {
    library
        .snapshot
        .verify_fact(&symbol.file_path, &symbol.file_content_hash)?;
    let bytes = library.snapshot.read_blob(&symbol.file_path)?;
    if symbol.byte_start >= symbol.byte_end || symbol.byte_end > bytes.len() {
        return Err(EvidenceError::StaleRange {
            path: symbol.file_path.clone(),
            detail: format!(
                "symbol byte range {}..{} is outside blob length {}",
                symbol.byte_start,
                symbol.byte_end,
                bytes.len()
            ),
        });
    }
    let excerpt =
        std::str::from_utf8(&bytes[symbol.byte_start..symbol.byte_end]).map_err(|_| {
            EvidenceError::StaleRange {
                path: symbol.file_path.clone(),
                detail: "symbol byte range splits UTF-8".to_string(),
            }
        })?;
    let excerpt_hash = gobby_core::indexing::content_hash(excerpt.as_bytes());
    if excerpt_hash != symbol.content_hash {
        return Err(EvidenceError::StaleRange {
            path: symbol.file_path.clone(),
            detail: format!(
                "symbol byte range has hash {excerpt_hash}, expected {}",
                symbol.content_hash
            ),
        });
    }
    let line_start = line_at(&bytes, symbol.byte_start);
    let line_end = line_at(&bytes, symbol.byte_end.saturating_sub(1));
    if line_start != symbol.line_start || line_end != symbol.line_end {
        return Err(EvidenceError::StaleRange {
            path: symbol.file_path.clone(),
            detail: format!(
                "symbol lines {}..{} resolve to {line_start}..{line_end}",
                symbol.line_start, symbol.line_end
            ),
        });
    }
    make_source(
        library,
        SourceSlice {
            path: &symbol.file_path,
            byte_start: symbol.byte_start,
            byte_end: symbol.byte_end,
            line_start,
            line_end,
            excerpt,
            qualified_name: Some(symbol.qualified_name.clone()),
        },
    )
}

pub(super) fn source_for_lines(
    library: &EvidenceLibrary,
    path: &str,
    start_line: usize,
    end_line: usize,
    qualified_name: Option<String>,
) -> Result<SourceEvidence> {
    validate_repo_path(path)?;
    if start_line == 0 || end_line < start_line {
        return Err(EvidenceError::InvalidSelector {
            detail: "line ranges are one-based, inclusive, and non-empty".to_string(),
        });
    }
    let bytes = library.snapshot.read_blob(path)?;
    let starts = line_starts(&bytes);
    if end_line > starts.len() {
        return Err(EvidenceError::StaleRange {
            path: path.to_string(),
            detail: format!(
                "requested lines {start_line}..{end_line}, blob has {} line(s)",
                starts.len()
            ),
        });
    }
    let byte_start = starts[start_line - 1];
    let byte_end = starts.get(end_line).copied().unwrap_or(bytes.len());
    let excerpt = std::str::from_utf8(&bytes[byte_start..byte_end]).map_err(|_| {
        EvidenceError::StaleRange {
            path: path.to_string(),
            detail: "line range is not UTF-8".to_string(),
        }
    })?;
    make_source(
        library,
        SourceSlice {
            path,
            byte_start,
            byte_end,
            line_start: start_line,
            line_end: end_line,
            excerpt,
            qualified_name,
        },
    )
}

struct SourceSlice<'a> {
    path: &'a str,
    byte_start: usize,
    byte_end: usize,
    line_start: usize,
    line_end: usize,
    excerpt: &'a str,
    qualified_name: Option<String>,
}

fn make_source(library: &EvidenceLibrary, source: SourceSlice<'_>) -> Result<SourceEvidence> {
    let SourceSlice {
        path,
        byte_start,
        byte_end,
        line_start,
        line_end,
        excerpt,
        qualified_name,
    } = source;
    let entry = library.snapshot.entry(path)?;
    let blob_oid = entry
        .blob_oid
        .clone()
        .ok_or_else(|| EvidenceError::ExcludedPath {
            path: path.to_string(),
            reason: entry
                .exclusion
                .unwrap_or(super::ExclusionReason::UnsupportedObject),
        })?;
    let content_hash = entry
        .content_hash
        .clone()
        .ok_or_else(|| EvidenceError::ExcludedPath {
            path: path.to_string(),
            reason: entry
                .exclusion
                .unwrap_or(super::ExclusionReason::UnsupportedObject),
        })?;
    let excerpt_hash = gobby_core::indexing::content_hash(excerpt.as_bytes());
    let evidence_id = format!(
        "src:{}",
        canonical_hash(&(
            library.snapshot.binding(),
            path,
            &blob_oid,
            byte_start,
            byte_end,
            &content_hash,
            &excerpt_hash,
            &qualified_name,
        ))?
    );
    Ok(SourceEvidence {
        evidence_id,
        path: path.to_string(),
        blob_oid,
        content_hash,
        excerpt_hash,
        qualified_name,
        line_start,
        line_end,
        byte_start,
        byte_end,
        excerpt: excerpt.to_string(),
    })
}

fn commit_items(library: &EvidenceLibrary) -> Result<Vec<EvidenceItem>> {
    let binding = library.snapshot.binding();
    let records = if binding.commit.changed_paths.is_empty() {
        vec![None]
    } else {
        binding
            .commit
            .changed_paths
            .iter()
            .cloned()
            .map(Some)
            .collect()
    };
    records
        .into_iter()
        .map(|changed_path| {
            let record_hash = canonical_hash(&changed_path)?;
            let evidence_id = format!(
                "commit:{}",
                canonical_hash(&(
                    &binding.commit_oid,
                    &binding.commit.parent_oids,
                    &binding.commit.comparison_parent_oid,
                    &binding.commit.comparison_kind,
                    &binding.commit.changed_paths_digest,
                    &record_hash,
                ))?
            );
            Ok(EvidenceItem::CommitMetadata(CommitMetadataEvidence {
                evidence_id,
                commit_oid: binding.commit_oid.clone(),
                parent_oids: binding.commit.parent_oids.clone(),
                comparison_parent_oid: binding.commit.comparison_parent_oid.clone(),
                comparison_kind: binding.commit.comparison_kind,
                changed_paths_digest: binding.commit.changed_paths_digest.clone(),
                changed_path_count: binding.commit.changed_paths.len(),
                changed_path,
                record_hash,
            }))
        })
        .collect()
}

fn line_starts(bytes: &[u8]) -> Vec<usize> {
    if bytes.is_empty() {
        return Vec::new();
    }
    let mut starts = vec![0];
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'\n' && index + 1 < bytes.len() {
            starts.push(index + 1);
        }
    }
    starts
}

fn line_at(bytes: &[u8], offset: usize) -> usize {
    1 + bytes[..offset.min(bytes.len())]
        .iter()
        .filter(|byte| **byte == b'\n')
        .count()
}
