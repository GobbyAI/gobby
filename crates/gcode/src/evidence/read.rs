use crate::codewiki_facts::SymbolFact;

use super::contracts::{
    CommitMetadataEvidence, Completeness, EvidenceItem, EvidenceWarning, ReadSelector,
    SourceEvidence,
};
use super::source::{canonical_hash, validate_repo_path};
use super::{EvidenceError, EvidenceLibrary, QueryResult, Result};

pub(super) fn execute(library: &EvidenceLibrary, selector: &ReadSelector) -> Result<QueryResult> {
    validate_selector(selector)?;
    let mut warnings = Vec::new();
    let items = match selector {
        ReadSelector::Range {
            path,
            start_line,
            end_line,
        } => {
            let (source, warning) = source_for_range(library, path, *start_line, *end_line)?;
            warnings.extend(warning);
            vec![EvidenceItem::Source(source)]
        }
        ReadSelector::Symbol {
            path,
            qualified_name,
        } => {
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
        ReadSelector::CommitMetadata { commit_oid } => commit_items(
            library,
            commit_oid.as_deref().unwrap_or(&library.binding.commit_oid),
        )?,
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
            ReadSelector::CommitMetadata { .. } => "read_commit_metadata",
            ReadSelector::Range { .. } => "read_range",
            ReadSelector::Symbol { .. } => "read_symbol",
        }
        .to_string(),
        hybrid: None,
        warnings,
        result_limit,
        graph_depth: None,
    })
}

pub(super) fn validate_selector(selector: &ReadSelector) -> Result<()> {
    match selector {
        ReadSelector::Range {
            path,
            start_line,
            end_line,
        } => {
            validate_repo_path(path)?;
            if *start_line == 0 || end_line < start_line {
                return Err(EvidenceError::InvalidSelector {
                    detail: "line ranges are one-based, inclusive, and non-empty".to_string(),
                });
            }
        }
        ReadSelector::Symbol {
            path,
            qualified_name,
        } => {
            validate_repo_path(path)?;
            if qualified_name.trim().is_empty() {
                return Err(EvidenceError::InvalidSelector {
                    detail: "qualified symbol name must not be empty".to_string(),
                });
            }
        }
        ReadSelector::CommitMetadata { commit_oid } => {
            if let Some(oid) = commit_oid
                && (oid.len() != 40 || !oid.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return Err(EvidenceError::InvalidSelector {
                    detail: "commit_oid must be a full 40-character commit object id".to_string(),
                });
            }
        }
    }
    Ok(())
}

pub(super) fn source_for_symbol(
    library: &EvidenceLibrary,
    symbol: &SymbolFact,
) -> Result<SourceEvidence> {
    library.verify_fact(&symbol.file_path, &symbol.file_content_hash)?;
    let bytes = library.read_file(&symbol.file_path)?;
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
    let bytes = library.read_file(path)?;
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
    slice_lines(
        library,
        path,
        &bytes,
        &starts,
        start_line,
        end_line,
        qualified_name,
    )
}

/// A caller-chosen range may overrun the file the way `sed -n` does: its end stops
/// at the last line with a warning. Index-derived ranges use `source_for_lines`,
/// where overrunning the file means the facts are stale.
fn source_for_range(
    library: &EvidenceLibrary,
    path: &str,
    start_line: usize,
    end_line: usize,
) -> Result<(SourceEvidence, Option<EvidenceWarning>)> {
    let bytes = library.read_file(path)?;
    let starts = line_starts(&bytes);
    let line_count = starts.len();
    if start_line > line_count {
        return Err(EvidenceError::InvalidSelector {
            detail: format!(
                "requested lines {start_line}..{end_line}, but {path} has {line_count} line(s)"
            ),
        });
    }
    let warning = (end_line > line_count).then(|| EvidenceWarning {
        code: "range_clamped_to_end_of_file".to_string(),
        message: format!(
            "requested lines {start_line}..{end_line}; file ends at line {line_count}"
        ),
        path: Some(path.to_string()),
    });
    let source = slice_lines(
        library,
        path,
        &bytes,
        &starts,
        start_line,
        end_line.min(line_count),
        None,
    )?;
    Ok((source, warning))
}

fn slice_lines(
    library: &EvidenceLibrary,
    path: &str,
    bytes: &[u8],
    starts: &[usize],
    start_line: usize,
    end_line: usize,
    qualified_name: Option<String>,
) -> Result<SourceEvidence> {
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
    let entry = library.entry(path)?;
    let content_hash = entry.content_hash.clone();
    let excerpt_hash = gobby_core::indexing::content_hash(excerpt.as_bytes());
    let evidence_id = format!(
        "src:{}",
        canonical_hash(&(
            &library.binding,
            path,
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
        content_hash,
        excerpt_hash,
        qualified_name,
        line_start,
        line_end,
        byte_start,
        byte_end,
        numbered_excerpt: numbered_excerpt(excerpt, line_start),
        excerpt: excerpt.to_string(),
    })
}

/// `excerpt` stays byte-exact for hash checks, so line numbers live in this copy:
/// readers cite a line without counting escaped newlines.
pub(super) fn numbered_excerpt(excerpt: &str, line_start: usize) -> String {
    excerpt
        .split_inclusive('\n')
        .zip(line_start..)
        .map(|(line, number)| format!("{number}| {line}"))
        .collect()
}

fn commit_items(library: &EvidenceLibrary, commit_oid: &str) -> Result<Vec<EvidenceItem>> {
    let commit = super::provenance::load_commit_binding(&library.repository_root, commit_oid)?;
    let records = if commit.changed_paths.is_empty() {
        vec![None]
    } else {
        commit.changed_paths.iter().cloned().map(Some).collect()
    };
    records
        .into_iter()
        .map(|changed_path| {
            let patch = match &changed_path {
                Some(path) => super::provenance::read_patch(
                    &library.repository_root,
                    &commit.comparison_parent_oid,
                    commit_oid,
                    path,
                )?,
                None => String::new(),
            };
            let record_hash = canonical_hash(&(&changed_path, &patch))?;
            let evidence_id = format!(
                "commit:{}",
                canonical_hash(&(
                    commit_oid,
                    &commit.parent_oids,
                    &commit.comparison_parent_oid,
                    &commit.comparison_kind,
                    &commit.changed_paths_digest,
                    &record_hash,
                ))?
            );
            Ok(EvidenceItem::CommitMetadata(CommitMetadataEvidence {
                evidence_id,
                commit_oid: commit_oid.to_string(),
                parent_oids: commit.parent_oids.clone(),
                comparison_parent_oid: commit.comparison_parent_oid.clone(),
                comparison_kind: commit.comparison_kind,
                changed_paths_digest: commit.changed_paths_digest.clone(),
                changed_path_count: commit.changed_paths.len(),
                changed_path,
                patch,
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
