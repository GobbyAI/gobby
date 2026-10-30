//! Embedding input for code symbols.
//!
//! Every symbol embeds its metadata. Functions and methods add their full
//! source slice from the stored byte range, uncapped. Classes and modules add
//! their member names instead, so nested bodies are never embedded twice.

use std::path::Path;

use crate::commands::symbols::read_symbol_source;
use crate::index::parser::{ImportResolutionContext, parse_source_with_semantic};
use crate::models::Symbol;

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
        "function" | "method" => {
            if let Some(source) = read_symbol_source(root, symbol)? {
                lines.push(format!("source:\n{}", source.text));
            }
        }
        "class" | "module" => {
            let members = member_names(root, symbol)?;
            if !members.is_empty() {
                lines.push(format!("members: {}", members.join(", ")));
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
    let Some(source) = read_symbol_source(root, symbol)? else {
        return Ok(Vec::new());
    };
    let parsed = parse_source_with_semantic(
        &root.join(&symbol.file_path),
        &symbol.project_id,
        root,
        source.text.into_bytes(),
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
    vector_text_for_symbol_in(Path::new(""), symbol).expect("fixture source is readable")
}
