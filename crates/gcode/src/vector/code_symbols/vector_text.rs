//! Embedding input for code symbols.
//!
//! Every symbol embeds its metadata. Functions and methods add their full
//! source slice from the stored byte range, uncapped. Classes and modules add
//! their member names instead, so nested bodies are never embedded twice.

use std::path::Path;

use anyhow::Context;

use super::types::VectorLifecycleError;
use crate::index::parser::{ImportResolutionContext, parse_source_with_semantic};
use crate::models::Symbol;

/// Returns the symbol's stored byte range from its indexed file version.
///
/// The file on disk must still hash to the indexed `file_content_hash`. Edited
/// or missing source would supply different bytes at the stored offsets, so the
/// symbol fails instead of embedding text that was never indexed. A mismatch is
/// the typed `IndexedSourceChanged`, which callers treat as a deferral rather
/// than an error.
fn indexed_source(root: &Path, symbol: &Symbol) -> anyhow::Result<String> {
    let bytes = match std::fs::read(root.join(&symbol.file_path)) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(VectorLifecycleError::IndexedSourceMissing {
                file_path: symbol.file_path.clone(),
            }
            .into());
        }
        Err(error) => {
            return Err(anyhow::Error::new(error)
                .context(format!("indexed source {} is unreadable", symbol.file_path)));
        }
    };
    let found = gobby_core::indexing::content_hash(&bytes);
    if found != symbol.file_content_hash {
        return Err(VectorLifecycleError::IndexedSourceChanged {
            file_path: symbol.file_path.clone(),
        }
        .into());
    }
    let range = bytes
        .get(symbol.byte_start..symbol.byte_end)
        .with_context(|| format!("symbol byte range is outside {}", symbol.file_path))?;
    Ok(String::from_utf8_lossy(range).into_owned())
}

fn metadata_lines(symbol: &Symbol) -> Vec<String> {
    let mut lines = vec![
        format!("name: {}", symbol.name),
        format!("qualified_name: {}", symbol.qualified_name),
        format!("kind: {}", symbol.kind),
        format!("language: {}", symbol.language),
        format!("file_path: {}", symbol.file_path),
        format!("range: {}-{}", symbol.line_start, symbol.line_end),
    ];
    if let Some(signature) = symbol
        .signature
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("signature: {signature}"));
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("docstring: {docstring}"));
    }
    if let Some(summary) = symbol
        .summary
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("summary: {summary}"));
    }
    lines
}

/// Embedding input for `symbol`, with `file_path` resolved under `root`.
pub(crate) fn vector_text_for_symbol_in(root: &Path, symbol: &Symbol) -> anyhow::Result<String> {
    let mut lines = metadata_lines(symbol);
    match symbol.kind.as_str() {
        // Type definitions (enums, traits, interfaces, aliases) carry their
        // meaning in variants and fields, so they embed their source too.
        "function" | "method" | "type" => {
            let source = indexed_source(root, symbol)?;
            lines.push(format!("source:\n{source}"));
        }
        "class" | "module" => {
            let members = member_names(root, symbol)?;
            if !members.is_empty() {
                lines.push(format!("members: {}", members.join(", ")));
            } else if symbol.kind == "class" {
                // A memberless class is a data shape (a Rust struct's fields).
                let source = indexed_source(root, symbol)?;
                lines.push(format!("source:\n{source}"));
            }
        }
        _ => {}
    }
    Ok(lines.join("\n"))
}

/// Change key for `symbol`'s embedding input, computed without reading source.
///
/// `content_hash` covers the symbol's byte range, so a body edit, a member
/// change, or any edit inside a container changes the key.
pub(crate) fn vector_input_fingerprint(symbol: &Symbol) -> String {
    let mut lines = metadata_lines(symbol);
    lines.push(format!("content_hash: {}", symbol.content_hash));
    lines.join("\n")
}

fn member_names(root: &Path, symbol: &Symbol) -> anyhow::Result<Vec<String>> {
    let source = indexed_source(root, symbol)?;
    let parsed = parse_source_with_semantic(
        &root.join(&symbol.file_path),
        &symbol.project_id,
        root,
        source.into_bytes(),
        &ImportResolutionContext::default(),
        None,
    )?;
    Ok(parsed
        .map(|result| result.symbols)
        .unwrap_or_default()
        .into_iter()
        .filter(|member| !(member.kind == symbol.kind && member.name == symbol.name))
        .map(|member| member.name)
        .collect())
}

#[cfg(test)]
pub(crate) fn vector_text_for_symbol(symbol: &Symbol) -> String {
    let root = super::tests::fixture_source_root();
    vector_text_for_symbol_in(root.path(), symbol).expect("fixture source is readable")
}
