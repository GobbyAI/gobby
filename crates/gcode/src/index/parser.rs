//! Tree-sitter AST parsing for symbol, import, and call extraction.
//! Ports logic from src/gobby/code_index/parser.py.

use std::collections::HashSet;
use std::path::Path;

use streaming_iterator::StreamingIterator;
use tree_sitter::{Parser, Query, QueryCursor};

use crate::index::hasher::symbol_content_hash;
use crate::index::languages;
use crate::index::security;
use crate::models::{CallRelation, ImportRelation, ParseResult, Symbol};

/// Maximum file size to index (10 MB).
const MAX_FILE_SIZE: u64 = 10 * 1024 * 1024;

/// Parse a single file into symbols, imports, and calls.
/// Returns None if the file should be skipped.
pub fn parse_file(
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
) -> Option<ParseResult> {
    // Security checks
    if !security::validate_path(file_path, root_path) {
        return None;
    }
    if !security::is_symlink_safe(file_path, root_path) {
        return None;
    }
    if security::should_exclude(file_path, exclude_patterns) {
        return None;
    }
    if security::has_secret_extension(file_path) {
        return None;
    }

    let meta = file_path.metadata().ok()?;
    if meta.len() == 0 || meta.len() > MAX_FILE_SIZE {
        return None;
    }

    if security::is_binary(file_path) {
        return None;
    }

    let file_str = file_path.to_string_lossy();
    let language = languages::detect_language(&file_str)?;
    let spec = languages::get_spec(language)?;
    let ts_lang = languages::get_ts_language(language)?;

    let source = std::fs::read(file_path).ok()?;

    let mut parser = Parser::new();
    parser.set_language(&ts_lang).ok()?;
    let tree = parser.parse(&source, None)?;

    let rel_path = file_path
        .canonicalize()
        .ok()
        .and_then(|abs| {
            root_path.canonicalize().ok().and_then(|root| {
                abs.strip_prefix(&root)
                    .ok()
                    .map(|p| p.to_string_lossy().to_string())
            })
        })
        .unwrap_or_else(|| file_str.to_string());

    let mut symbols = extract_symbols(
        &tree, &source, spec, language, &ts_lang, project_id, &rel_path,
    );
    link_parents(&mut symbols);
    let imports = extract_imports(&tree, &source, spec, &ts_lang, &rel_path);
    let calls = extract_calls(&tree, &source, spec, &ts_lang, &rel_path, &symbols);

    Some(ParseResult {
        symbols,
        imports,
        calls,
    })
}

fn extract_symbols(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    language: &str,
    ts_lang: &tree_sitter::Language,
    project_id: &str,
    rel_path: &str,
) -> Vec<Symbol> {
    if spec.symbol_query.trim().is_empty() {
        return Vec::new();
    }

    let query = match Query::new(ts_lang, spec.symbol_query) {
        Ok(q) => q,
        Err(_) => return Vec::new(),
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);

    let mut symbols = Vec::new();
    let mut seen_ids = HashSet::new();
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();

    while let Some(m) = matches.next() {
        let mut name_text: Option<String> = None;
        let mut def_node = None;
        let mut kind = String::from("function");

        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "name" {
                name_text = Some(
                    String::from_utf8_lossy(&source[cap.node.start_byte()..cap.node.end_byte()])
                        .to_string(),
                );
            } else if let Some(k) = cap_name.strip_prefix("definition.") {
                def_node = Some(cap.node);
                kind = k.to_string();
            }
        }

        let (name, node) = match (name_text, def_node) {
            (Some(n), Some(d)) => (n, d),
            _ => continue,
        };

        // Signature: first line of definition
        let sig_end = source[node.start_byte()..]
            .iter()
            .position(|&b| b == b'\n')
            .map(|p| node.start_byte() + p)
            .unwrap_or(node.end_byte());
        let mut signature = String::from_utf8_lossy(&source[node.start_byte()..sig_end])
            .trim()
            .to_string();
        if signature.len() > 200 {
            signature.truncate(200);
            signature.push_str("...");
        }

        let docstring = extract_docstring(&node, source, language);
        let c_hash = symbol_content_hash(source, node.start_byte(), node.end_byte());
        let symbol_id = Symbol::make_id(project_id, rel_path, &name, &kind, node.start_byte());

        if seen_ids.contains(&symbol_id) {
            continue;
        }
        seen_ids.insert(symbol_id.clone());

        symbols.push(Symbol {
            id: symbol_id,
            project_id: project_id.to_string(),
            file_path: rel_path.to_string(),
            name: name.clone(),
            qualified_name: name,
            kind,
            language: language.to_string(),
            byte_start: node.start_byte(),
            byte_end: node.end_byte(),
            line_start: node.start_position().row + 1,
            line_end: node.end_position().row + 1,
            signature: Some(signature),
            docstring,
            parent_symbol_id: None,
            content_hash: c_hash,
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        });
    }

    symbols
}

fn link_parents(symbols: &mut [Symbol]) {
    let mut indices: Vec<usize> = (0..symbols.len()).collect();
    indices.sort_by_key(|&i| symbols[i].byte_start);

    for idx in 0..indices.len() {
        let i = indices[idx];
        for jdx in (0..idx).rev() {
            let j = indices[jdx];
            let parent_kind = symbols[j].kind.as_str();
            if (parent_kind == "class" || parent_kind == "type")
                && symbols[j].byte_start <= symbols[i].byte_start
                && symbols[j].byte_end >= symbols[i].byte_end
            {
                let parent_name = symbols[j].name.clone();
                let parent_id = symbols[j].id.clone();
                let sym = &mut symbols[i];
                sym.parent_symbol_id = Some(parent_id);
                sym.qualified_name = format!("{}.{}", parent_name, sym.name);
                if sym.kind == "function" {
                    sym.kind = "method".to_string();
                }
                break;
            }
        }
    }
}

fn extract_docstring(node: &tree_sitter::Node, source: &[u8], language: &str) -> Option<String> {
    if !matches!(language, "python" | "javascript" | "typescript") {
        return None;
    }

    let mut body = None;
    let mut walk = node.walk();
    for child in node.children(&mut walk) {
        let ty = child.kind();
        if ty == "block" || ty == "statement_block" {
            body = Some(child);
            break;
        }
    }
    let body = body?;

    let mut walk2 = body.walk();
    for child in body.children(&mut walk2) {
        let ty = child.kind();
        if ty == "comment" || ty == "\n" || ty == "newline" {
            continue;
        }

        let string_node = if ty == "string" {
            Some(child)
        } else if ty == "expression_statement" {
            let mut w3 = child.walk();
            child.children(&mut w3).find(|gc| gc.kind() == "string")
        } else {
            None
        };

        let string_node = string_node?;

        // Try string_content child first
        let mut w4 = string_node.walk();
        for sc in string_node.children(&mut w4) {
            if sc.kind() == "string_content" {
                let raw = String::from_utf8_lossy(&source[sc.start_byte()..sc.end_byte()]);
                let trimmed = raw.trim();
                return if trimmed.is_empty() {
                    None
                } else {
                    Some(trimmed.to_string())
                };
            }
        }

        // Fallback: strip quotes
        let raw =
            String::from_utf8_lossy(&source[string_node.start_byte()..string_node.end_byte()]);
        let raw = raw.trim();
        let stripped = strip_quotes(raw);
        return if stripped.is_empty() {
            None
        } else {
            Some(stripped.to_string())
        };
    }

    None
}

fn strip_quotes(s: &str) -> &str {
    for q in &["\"\"\"", "'''", "\"", "'"] {
        if s.starts_with(q) && s.ends_with(q) && s.len() >= q.len() * 2 {
            return s[q.len()..s.len() - q.len()].trim();
        }
    }
    s
}

fn extract_imports(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ts_lang: &tree_sitter::Language,
    rel_path: &str,
) -> Vec<ImportRelation> {
    if spec.import_query.trim().is_empty() {
        return Vec::new();
    }

    let query = match Query::new(ts_lang, spec.import_query) {
        Ok(q) => q,
        Err(_) => return Vec::new(),
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();
    let mut imports = Vec::new();

    while let Some(m) = matches.next() {
        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "import" {
                let text =
                    String::from_utf8_lossy(&source[cap.node.start_byte()..cap.node.end_byte()])
                        .trim()
                        .to_string();
                imports.push(ImportRelation {
                    file_path: rel_path.to_string(),
                    module_name: text,
                });
            }
        }
    }

    imports
}

fn extract_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ts_lang: &tree_sitter::Language,
    rel_path: &str,
    symbols: &[Symbol],
) -> Vec<CallRelation> {
    if spec.call_query.trim().is_empty() {
        return Vec::new();
    }

    let query = match Query::new(ts_lang, spec.call_query) {
        Ok(q) => q,
        Err(_) => return Vec::new(),
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();
    let mut calls = Vec::new();

    while let Some(m) = matches.next() {
        let mut name_node = None;
        let mut call_node = None;

        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "name" {
                name_node = Some(cap.node);
            } else if cap_name == "call" {
                call_node = Some(cap.node);
            }
        }

        let name_n = match name_node {
            Some(n) => n,
            None => continue,
        };

        let callee_name =
            String::from_utf8_lossy(&source[name_n.start_byte()..name_n.end_byte()]).to_string();

        let target = call_node.unwrap_or(name_n);
        let caller_id = symbols
            .iter()
            .rfind(|s| s.byte_start <= target.start_byte() && target.start_byte() <= s.byte_end)
            .map(|s| s.id.clone())
            .unwrap_or_default();

        calls.push(CallRelation {
            caller_id,
            callee_name,
            file_path: rel_path.to_string(),
            line: name_n.start_position().row + 1,
        });
    }

    calls
}
