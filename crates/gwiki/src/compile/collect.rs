use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::WikiError;
use crate::session::ResearchSession;

use super::*;

pub(crate) fn collect_accepted_sources(
    session: &ResearchSession,
) -> Result<CollectedSources, WikiError> {
    let mut accepted_sources = Vec::new();
    let mut citations = Vec::new();
    let mut conflicting_claims = Vec::new();
    let mut missing_evidence = Vec::new();

    for note in &session.accepted_notes {
        let path = note_path(session.scope.root(), &note.path);
        match path.try_exists() {
            Ok(true) => {}
            Ok(false) => {
                return Err(WikiError::NotFound {
                    resource: "accepted_note",
                    id: path.display().to_string(),
                });
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "check accepted research note",
                    path: Some(path.clone()),
                    source: error,
                });
            }
        }
        require_path_in_scope(&path, session.scope.root())?;
        let text = match fs::read_to_string(&path) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Err(WikiError::NotFound {
                    resource: "accepted_note",
                    id: path.display().to_string(),
                });
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "read accepted research note",
                    path: Some(path.clone()),
                    source: error,
                });
            }
        };
        let note_sections = parse_note_sections(&text);
        extend_unique(&mut citations, note_sections.citations);
        extend_unique(&mut conflicting_claims, note_sections.conflicting_claims);
        extend_unique(&mut missing_evidence, note_sections.missing_evidence);
        accepted_sources.push(AcceptedCompileSource {
            title: note.title.clone(),
            path,
            chunks: note_sections
                .chunks
                .iter()
                .map(|chunk| chunk.text.clone())
                .collect(),
            chunk_offsets: note_sections
                .chunks
                .iter()
                .map(|chunk| AcceptedCompileChunkOffset {
                    byte_start: chunk.byte_start,
                    byte_end: chunk.byte_end,
                })
                .collect(),
        });
    }

    Ok(CollectedSources {
        accepted_sources,
        citations,
        conflicting_claims,
        missing_evidence,
    })
}

#[derive(Debug, Default)]
struct ParsedNoteSections {
    citations: Vec<String>,
    conflicting_claims: Vec<String>,
    missing_evidence: Vec<String>,
    chunks: Vec<ParsedNoteChunk>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ParsedNoteChunk {
    text: String,
    byte_start: usize,
    byte_end: usize,
}

fn parse_note_sections(text: &str) -> ParsedNoteSections {
    let mut sections = ParsedNoteSections::default();
    for (line_start, line) in body_line_spans(text) {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["citation:"]) {
            sections.citations.push(value.to_string());
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["conflict:", "conflicting claim:"]) {
            sections.conflicting_claims.push(value.to_string());
            continue;
        }
        if let Some(value) = prefixed_value(trimmed, &["gap:", "missing evidence:"]) {
            sections.missing_evidence.push(value.to_string());
            continue;
        }
        let leading = line.len() - line.trim_start().len();
        let trailing = line.len() - line.trim_end().len();
        sections.chunks.push(ParsedNoteChunk {
            text: trimmed.to_string(),
            byte_start: line_start + leading,
            byte_end: line_start + line.len() - trailing,
        });
    }
    sections
}

fn body_line_spans(text: &str) -> Vec<(usize, &str)> {
    let body_start = body_start_offset(text);
    let mut spans = Vec::new();
    let mut offset = body_start;
    for segment in text[body_start..].split_inclusive('\n') {
        let without_newline = segment.strip_suffix('\n').unwrap_or(segment);
        let line = without_newline
            .strip_suffix('\r')
            .unwrap_or(without_newline);
        spans.push((offset, line));
        offset += segment.len();
    }
    spans
}

fn body_start_offset(text: &str) -> usize {
    let Some(first_line_end) = text.find('\n') else {
        return 0;
    };
    if text[..first_line_end].trim() != "---" {
        return 0;
    }

    let mut offset = first_line_end + 1;
    while offset < text.len() {
        let line_end = text[offset..]
            .find('\n')
            .map_or(text.len(), |relative| offset + relative);
        let line = text[offset..line_end].trim_end_matches('\r');
        if line.trim() == "---" {
            return if line_end < text.len() {
                line_end + 1
            } else {
                line_end
            };
        }
        if line_end == text.len() {
            return text.len();
        }
        offset = line_end + 1;
    }
    text.len()
}

fn prefixed_value<'a>(line: &'a str, prefixes: &[&str]) -> Option<&'a str> {
    // Research notes may vary heading/list capitalization; match labels case-insensitively.
    let lower = line.to_ascii_lowercase();
    for prefix in prefixes {
        if lower.starts_with(prefix) {
            let value = line[prefix.len()..].trim();
            if !value.is_empty() {
                return Some(value);
            }
        }
    }
    None
}

pub(crate) fn extend_unique(target: &mut Vec<String>, values: Vec<String>) {
    let mut seen = HashSet::with_capacity(target.len() + values.len());
    seen.extend(target.iter().cloned());
    for value in values {
        if seen.insert(value.clone()) {
            target.push(value);
        }
    }
}

fn note_path(root: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    }
}

/// Canonicalizes both paths so symlinks are resolved before the scope check.
/// Returns `WikiError::InvalidInput` when the note resolves outside the wiki root.
fn require_path_in_scope(path: &Path, root: &Path) -> Result<(), WikiError> {
    let root = match root.canonicalize() {
        Ok(root) => root,
        Err(error) => {
            return Err(WikiError::Io {
                action: "canonicalize wiki scope root",
                path: Some(root.to_path_buf()),
                source: error,
            });
        }
    };
    let path = match path.canonicalize() {
        Ok(path) => path,
        Err(error) => {
            return Err(WikiError::Io {
                action: "canonicalize accepted research note",
                path: Some(path.to_path_buf()),
                source: error,
            });
        }
    };
    if path.starts_with(&root) {
        return Ok(());
    }
    Err(WikiError::InvalidInput {
        field: "accepted_note",
        message: format!(
            "accepted research note {} is outside wiki scope {}",
            path.display(),
            root.display()
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extend_unique_preserves_order_and_removes_existing_or_new_duplicates() {
        let mut target = vec!["alpha".to_string(), "beta".to_string()];

        extend_unique(
            &mut target,
            vec![
                "beta".to_string(),
                "gamma".to_string(),
                "alpha".to_string(),
                "delta".to_string(),
                "gamma".to_string(),
            ],
        );

        assert_eq!(
            target,
            vec![
                "alpha".to_string(),
                "beta".to_string(),
                "gamma".to_string(),
                "delta".to_string(),
            ]
        );
    }

    #[test]
    fn missing_accepted_note_returns_not_found() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut session = ResearchSession::new(
            "question",
            crate::session::ResearchScope::topic("topic", temp.path()),
            Vec::new(),
            1,
            None,
        )
        .expect("session");
        session
            .accepted_notes
            .push(crate::session::AcceptedResearchNote {
                title: "Missing".to_string(),
                path: PathBuf::from("raw/research/missing.md"),
                code_citations: Vec::new(),
                degradation: None,
            });

        let error = collect_accepted_sources(&session).expect_err("missing note must fail");

        assert!(matches!(
            error,
            WikiError::NotFound {
                resource: "accepted_note",
                ..
            }
        ));
    }
}
