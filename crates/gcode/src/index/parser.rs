//! Tree-sitter AST parsing for symbol, import, and call extraction.
//! Ports logic from src/gobby/code_index/parser.py.

use std::collections::HashSet;
use std::path::Path;

use streaming_iterator::StreamingIterator;
use tree_sitter::{Parser, Query, QueryCursor};

use crate::index::hasher::symbol_content_hash;
use crate::index::import_resolution::{self, ExtractedImports, ImportBindings};
use crate::index::languages;
use crate::index::security;
use crate::models::{CallRelation, ParseResult, Symbol};

pub use crate::index::import_resolution::{
    ImportResolutionContext, build_import_resolution_context,
};

/// Maximum file size to index (10 MB).
const MAX_FILE_SIZE: u64 = 10 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CallSyntaxKind {
    Bare,
    Member,
    Other,
}

/// Parse a single file into symbols, imports, and calls.
/// Returns None if the file should be skipped.
pub fn parse_file(
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
    import_context: &ImportResolutionContext,
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
    let extracted_imports = extract_imports(
        &tree,
        &source,
        spec,
        language,
        &ts_lang,
        &rel_path,
        import_context,
    );
    let calls = extract_calls(
        &tree,
        &source,
        spec,
        &ts_lang,
        &rel_path,
        &symbols,
        &extracted_imports.bindings,
    );

    Some(ParseResult {
        symbols,
        imports: extracted_imports.imports,
        calls,
        source,
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
        let c_hash =
            symbol_content_hash(source, node.start_byte(), node.end_byte()).unwrap_or_default();
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
    language: &str,
    ts_lang: &tree_sitter::Language,
    rel_path: &str,
    import_context: &ImportResolutionContext,
) -> ExtractedImports {
    if spec.import_query.trim().is_empty() {
        return ExtractedImports::default();
    }

    let query = match Query::new(ts_lang, spec.import_query) {
        Ok(q) => q,
        Err(_) => return ExtractedImports::default(),
    };

    let mut cursor = QueryCursor::new();
    let mut matches = cursor.matches(&query, tree.root_node(), source);
    let capture_names: Vec<String> = query
        .capture_names()
        .iter()
        .map(|s| s.to_string())
        .collect();
    let mut extracted = ExtractedImports::default();

    while let Some(m) = matches.next() {
        for cap in m.captures {
            let cap_name = &capture_names[cap.index as usize];
            if cap_name == "import" {
                let text =
                    String::from_utf8_lossy(&source[cap.node.start_byte()..cap.node.end_byte()])
                        .trim()
                        .to_string();
                import_resolution::parse_import_statement(
                    language,
                    &text,
                    rel_path,
                    import_context,
                    &mut extracted,
                );
            }
        }
    }

    extracted
}

fn extract_calls(
    tree: &tree_sitter::Tree,
    source: &[u8],
    spec: &languages::LanguageSpec,
    ts_lang: &tree_sitter::Language,
    rel_path: &str,
    symbols: &[Symbol],
    import_bindings: &ImportBindings,
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
        let caller_symbol = enclosing_symbol(symbols, target.start_byte());
        let caller_symbol_id = caller_symbol.map(|s| s.id.clone()).unwrap_or_default();
        let syntax = call_syntax_kind(name_n, target);
        let local_target = resolve_same_file_callee(symbols, caller_symbol, &callee_name, syntax);
        let external_target = import_resolution::resolve_external_callee(
            import_bindings,
            symbols,
            &callee_name,
            member_root_alias(source, target, name_n).as_deref(),
            syntax == CallSyntaxKind::Bare,
        );

        let mut call = CallRelation::new(
            caller_symbol_id,
            callee_name,
            rel_path.to_string(),
            name_n.start_position().row + 1,
        );
        match (local_target, external_target) {
            (Some(callee_symbol_id), None) => {
                call = call.with_symbol_target(callee_symbol_id);
            }
            (None, Some(external_target)) => {
                call =
                    call.with_external_target(external_target.callee_name, external_target.module);
            }
            _ => {}
        }
        calls.push(call);
    }

    calls
}

fn enclosing_symbol(symbols: &[Symbol], byte_offset: usize) -> Option<&Symbol> {
    symbols
        .iter()
        .rfind(|s| s.byte_start <= byte_offset && byte_offset <= s.byte_end)
}

fn call_syntax_kind(name_node: tree_sitter::Node, call_node: tree_sitter::Node) -> CallSyntaxKind {
    let Some(mut ancestor) = name_node.parent() else {
        return CallSyntaxKind::Other;
    };
    if ancestor.id() == call_node.id() {
        return CallSyntaxKind::Bare;
    }

    loop {
        if is_memberish_kind(ancestor.kind()) {
            return CallSyntaxKind::Member;
        }
        if ancestor.id() == call_node.id() {
            return CallSyntaxKind::Other;
        }
        let Some(parent) = ancestor.parent() else {
            return CallSyntaxKind::Other;
        };
        ancestor = parent;
    }
}

fn is_memberish_kind(kind: &str) -> bool {
    matches!(
        kind,
        "attribute"
            | "member_expression"
            | "selector_expression"
            | "field_expression"
            | "member_access_expression"
            | "member_call_expression"
    )
}

fn is_callable_kind(kind: &str) -> bool {
    matches!(kind, "function" | "method")
}

fn resolve_same_file_callee(
    symbols: &[Symbol],
    caller_symbol: Option<&Symbol>,
    callee_name: &str,
    syntax: CallSyntaxKind,
) -> Option<String> {
    match syntax {
        CallSyntaxKind::Bare => unique_symbol_id(
            symbols
                .iter()
                .filter(|symbol| symbol.name == callee_name && is_callable_kind(&symbol.kind)),
        ),
        CallSyntaxKind::Member => {
            let parent_symbol_id =
                caller_symbol.and_then(|symbol| symbol.parent_symbol_id.as_deref())?;
            unique_symbol_id(symbols.iter().filter(|symbol| {
                symbol.name == callee_name
                    && symbol.kind == "method"
                    && symbol.parent_symbol_id.as_deref() == Some(parent_symbol_id)
            }))
        }
        CallSyntaxKind::Other => None,
    }
}

fn unique_symbol_id<'a>(symbols: impl Iterator<Item = &'a Symbol>) -> Option<String> {
    let mut symbols = symbols;
    let first = symbols.next()?;
    if symbols.next().is_some() {
        None
    } else {
        Some(first.id.clone())
    }
}

fn member_root_alias(
    source: &[u8],
    call_node: tree_sitter::Node,
    name_node: tree_sitter::Node,
) -> Option<String> {
    let prefix = String::from_utf8_lossy(&source[call_node.start_byte()..name_node.start_byte()]);
    let mut chars = prefix.chars().skip_while(|c| !is_identifier_start(*c));
    let first = chars.next()?;
    if !is_identifier_start(first) {
        return None;
    }

    let mut alias = String::from(first);
    for ch in chars {
        if is_identifier_continue(ch) {
            alias.push(ch);
        } else {
            break;
        }
    }

    Some(alias)
}

fn is_identifier_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || matches!(ch, '_' | '$')
}

fn is_identifier_continue(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || matches!(ch, '_' | '$')
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};

    use tempfile::TempDir;

    use super::*;

    fn parse_source(file_name: &str, source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        let tempdir = TempDir::new().expect("create tempdir");
        let root = tempdir.path();
        for (path, contents) in extra_files {
            let file_path = root.join(path);
            if let Some(parent) = file_path.parent() {
                fs::create_dir_all(parent).expect("create parent dirs");
            }
            fs::write(&file_path, contents).expect("write extra source");
        }

        let path = root.join(file_name);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create parent dirs");
        }
        fs::write(&path, source).expect("write test source");
        let candidates = discover_supported_files(root);
        let context = build_import_resolution_context(root, &candidates);
        parse_file(&path, "proj", root, &[], &context).expect("parse file")
    }

    fn parse_python(source: &str) -> ParseResult {
        parse_source("sample.py", source, &[])
    }

    fn parse_javascript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/sample.js", source, extra_files)
    }

    fn parse_typescript(source: &str, extra_files: &[(&str, &str)]) -> ParseResult {
        parse_source("src/sample.ts", source, extra_files)
    }

    fn discover_supported_files(root: &Path) -> Vec<PathBuf> {
        let mut candidates = Vec::new();
        let mut stack = vec![root.to_path_buf()];
        while let Some(path) = stack.pop() {
            let entries = fs::read_dir(&path).expect("read dir");
            for entry in entries {
                let entry = entry.expect("dir entry");
                let entry_path = entry.path();
                if entry_path.is_dir() {
                    stack.push(entry_path);
                } else if let Some(language) =
                    languages::detect_language(&entry_path.to_string_lossy())
                    && !language.is_empty()
                {
                    candidates.push(entry_path);
                }
            }
        }
        candidates
    }

    #[test]
    fn resolves_unique_same_file_bare_calls() {
        let parsed = parse_python(
            r#"
def foo():
    pass

def bar():
    foo()
"#,
        );

        let foo = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == "foo")
            .expect("foo symbol");
        let bar = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.name == "bar")
            .expect("bar symbol");
        let call = parsed.calls.first().expect("call");

        assert_eq!(call.caller_symbol_id, bar.id);
        assert_eq!(call.callee_symbol_id.as_deref(), Some(foo.id.as_str()));
    }

    #[test]
    fn resolves_same_class_member_calls() {
        let parsed = parse_python(
            r#"
class Greeter:
    def greet(self):
        self.render()

    def render(self):
        pass
"#,
        );

        let render = parsed
            .symbols
            .iter()
            .find(|symbol| symbol.qualified_name == "Greeter.render")
            .expect("render method");
        let call = parsed.calls.first().expect("call");

        assert_eq!(call.callee_symbol_id.as_deref(), Some(render.id.as_str()));
    }

    #[test]
    fn leaves_ambiguous_bare_calls_unresolved() {
        let parsed = parse_python(
            r#"
def foo():
    pass

class A:
    def foo(self):
        pass

def bar():
    foo()
"#,
        );

        let call = parsed.calls.first().expect("call");
        assert!(call.callee_symbol_id.is_none());
    }

    #[test]
    fn leaves_non_local_member_calls_unresolved() {
        let parsed = parse_python(
            r#"
class A:
    def bar(self):
        obj.render()

class B:
    def render(self):
        pass
"#,
        );

        let call = parsed.calls.first().expect("call");
        assert!(call.callee_symbol_id.is_none());
    }

    #[test]
    fn classifies_external_python_from_import_calls() {
        let parsed = parse_python(
            r#"
from requests import get as fetch

def run():
    fetch()
"#,
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "get");
        assert_eq!(call.callee_external_module.as_deref(), Some("requests"));
    }

    #[test]
    fn leaves_local_python_imports_unresolved() {
        let parsed = parse_source(
            "pkg/main.py",
            r#"
from pkg.utils import helper

def run():
    helper()
"#,
            &[("pkg/utils.py", "def helper():\n    pass\n")],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
        assert!(call.callee_external_module.is_none());
    }

    #[test]
    fn classifies_external_javascript_named_import_calls() {
        let parsed = parse_javascript(
            r#"
import { useState } from "react";

function run() {
  useState();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "useState");
        assert_eq!(call.callee_external_module.as_deref(), Some("react"));
    }

    #[test]
    fn classifies_external_javascript_namespace_member_calls() {
        let parsed = parse_javascript(
            r#"
import * as React from "react";

function run() {
  React.useState();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "useState");
        assert_eq!(call.callee_external_module.as_deref(), Some("react"));
    }

    #[test]
    fn leaves_relative_javascript_imports_unresolved() {
        let parsed = parse_javascript(
            r#"
import { helper } from "./utils";

function run() {
  helper();
}
"#,
            &[
                (
                    "package.json",
                    r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
                ),
                ("src/utils.js", "export function helper() {}\n"),
            ],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }

    #[test]
    fn classifies_external_typescript_default_member_calls() {
        let parsed = parse_typescript(
            r#"
import React from "react";

function run() {
  React.useState();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "external");
        assert_eq!(call.callee_name, "useState");
        assert_eq!(call.callee_external_module.as_deref(), Some("react"));
    }

    #[test]
    fn leaves_unlisted_javascript_package_aliases_unresolved() {
        let parsed = parse_javascript(
            r#"
import { helper } from "@/utils";

function run() {
  helper();
}
"#,
            &[(
                "package.json",
                r#"{"name":"app","dependencies":{"react":"^18.0.0"}}"#,
            )],
        );

        let call = parsed.calls.first().expect("call");
        assert_eq!(call.callee_target_kind.as_str(), "unresolved");
    }
}
